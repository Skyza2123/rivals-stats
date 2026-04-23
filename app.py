import csv
import copy
import io
import os
import json
import math
import re
import time
import zipfile
import hashlib
import sqlite3
import importlib
from difflib import SequenceMatcher
from itertools import combinations
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from uuid import uuid4
from flask import Flask, render_template, request, redirect, url_for, abort, g, flash, jsonify, has_request_context, session, Response
from markupsafe import Markup
from werkzeug.datastructures import FileStorage
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from data import (
    HEROES, HERO_ROLES, HERO_TRANSFORMATIONS, MAPS, MAP_IMAGES, MAP_SUBMAPS,
    SIDES, RESULTS, EVENT_TYPES, ATTACK_DEFENSE_MAPS, MAP_MODES, MAP_TYPES,
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")

# Serve static assets reliably behind WSGI hosts (Render/Gunicorn) when available.
_whitenoise_module = importlib.util.find_spec("whitenoise")
if _whitenoise_module is not None:
    from whitenoise import WhiteNoise

    app.wsgi_app = WhiteNoise(app.wsgi_app, root=str(Path(app.root_path) / "static"), prefix="static/")

def _default_database_path() -> Path:
    configured = (os.environ.get("DATABASE_PATH") or "").strip()
    if configured:
        return Path(configured)

    # Render filesystem is ephemeral unless writing to mounted disk.
    render_mount = (os.environ.get("RENDER_DISK_MOUNT_PATH") or "").strip()
    if render_mount:
        return Path(render_mount) / "rivals_stats.db"

    if (os.environ.get("RENDER") or "").strip().lower() == "true":
        render_default_mount = Path("/var/data")
        if render_default_mount.exists() and os.access(render_default_mount, os.W_OK):
            return render_default_mount / "rivals_stats.db"

    return Path(app.root_path) / "rivals_stats.db"


DB_PATH = _default_database_path()


def _default_logo_dir() -> Path:
    """Return the directory where team logo files should be stored.

    On Render (or any environment where the DB lives on a persistent disk)
    we co-locate logos with the database so they survive redeploys.  In
    every other environment logos go into the conventional static folder.
    """
    configured = (os.environ.get("LOGO_DIR") or "").strip()
    if configured:
        return Path(configured)

    render_mount = (os.environ.get("RENDER_DISK_MOUNT_PATH") or "").strip()
    if render_mount:
        return Path(render_mount) / "team_logos"

    if (os.environ.get("RENDER") or "").strip().lower() == "true":
        render_default_mount = Path("/var/data")
        if render_default_mount.exists() and os.access(render_default_mount, os.W_OK):
            return render_default_mount / "team_logos"

    return Path(app.static_folder) / "uploads" / "team_logos"


TEAM_LOGO_DIR = _default_logo_dir()
# True when logos live outside the static folder (persistent disk on Render).
_LOGOS_ON_DISK = TEAM_LOGO_DIR != Path(app.static_folder) / "uploads" / "team_logos"
PLAYER_ROLES = ["Vanguard", "Duelist", "Strategist", "Flex"]
TEAM_SLOTS = ["team1", "team2"]
TEAM_QUALITY_TAG_OPTIONS = ("Preferred", "Semi Preferred", "Good", "Avoid")
DEFAULT_MAP_TYPE = "Standard"
MAP_TYPE_ALIASES = {
    "standard": "Standard",
    "scrim": "Standard",
    "ptw": "PTW",
    "test": "Test",
    "trial": "Test",
}
HERO_NAME_ALIASES = {
    "adam": "Adam Warlock",
    "adamwarlock": "Adam Warlock",
    "bucky": "Winter Soldier",
    "buckybarnes": "Winter Soldier",
    "captainamerica": "Captain America",
    "captianamerica": "Captain America",
    "cap": "Captain America",
    "cloak": "Cloak & Dagger",
    "cloakdagger": "Cloak & Dagger",
    "deadpools": "SupportPool",
    "deadpoolsupport": "SupportPool",
    "deadpoolt": "Tankpool",
    "deadpooltank": "Tankpool",
    "deadpoolvanguard": "Tankpool",
    "dp": "DpsPool",
    "dpd": "DpsPool",
    "dps": "SupportPool",
    "dpspool": "DpsPool",
    "dpt": "Tankpool",
    "dpss": "SupportPool",
    "daredevil": "Daredevil",
    "devil": "Daredevil",
    "doctorstrange": "Dr. Strange",
    "drstrange": "Dr. Strange",
    "emma": "Emma Frost",
    "elsa": "Elsa Bloodstone",
    "fox": "White Fox",
    "fist": "Iron Fist",
    "hawk": "Hawkeye",
    "human": "Human Torch",
    "invisiblewoman": "Invisible Woman",
    "iw": "Invisible Woman",
    "jeff": "Jeff TLS",
    "jeffthelandshark": "Jeff TLS",
    "jefftls": "Jeff TLS",
    "landshark": "Jeff TLS",
    "luna": "Luna Snow",
    "mag": "Magneto",
    "moon": "Moon Knight",
    "peni": "Peni Parker",
    "penni": "Peni Parker",
    "psy": "Psylocke",
    "puni": "Punisher",
    "rocket": "Rocket Raccoon",
    "rocketracoon": "Rocket Raccoon",
    "sue": "Invisible Woman",
    "starlord": "Star-Lord",
    "star": "Star-Lord",
    "strange": "Dr. Strange",
    "supportpool": "SupportPool",
    "thething": "Thing",
    "thing": "Thing",
    "tankpool": "Tankpool",
    "wintersoldier": "Winter Soldier",
    "wintersolider": "Winter Soldier",
    "wolve": "Wolverine",
}
PLAYER_NAME_ALIASES = {
    "drstrange": "Dr Strange",
}
_RINGER_NAME_MARKERS = (
    "r",
    "ringer",
    "standin",
    "stand-in",
    "sub",
    "substitute",
    "merc",
    "mercenary",
)
_RINGER_NAME_MARKER_KEYS = {re.sub(r"[^a-z0-9]+", "", marker.lower()) for marker in _RINGER_NAME_MARKERS}
DRAFT_SLOT_ORDER = ("ban1", "protect1", "ban2", "ban3", "protect2", "ban4")
PREDICTOR_INPUT_ORDER = (
    "t1_ban1",
    "t2_ban1",
    "t2_protect1",
    "t1_ban2",
    "t1_protect1",
    "t1_ban3",
    "t2_ban2",
    "t1_protect2",
    "t2_ban3",
    "t2_ban4",
    "t2_protect2",
    "t1_ban4",
)
SIMULATOR_SLOT_ORDER = (
    "team1_ban1",
    "team2_ban1",
    "team1_ban2",
    "team2_protect1",
    "team2_ban2",
    "team1_protect1",
    "team1_ban3",
    "team2_ban3",
    "team1_protect2",
    "team2_ban4",
    "team2_protect2",
    "team1_ban4",   
   
)
CONCEPT_ONE_SIDED_SLOT_ORDER = (
    "my_ban1",
    "their_protect1",
    "my_ban2",
    "my_ban3",
    "their_protect2",
    "my_ban4",
)
PREDICTOR_GROUPS = (
    (("team1", "ban1", "t1_ban1"), ("team2", "ban1", "t2_ban1")),
    (("team2", "protect1", "t2_protect1"),),
    (("team1", "ban2", "t1_ban2"), ("team1", "protect1", "t1_protect1")),
    (("team1", "ban3", "t1_ban3"), ("team2", "ban2", "t2_ban2")),
    (("team1", "protect2", "t1_protect2"),),
    (("team2", "ban3", "t2_ban3"), ("team2", "ban4", "t2_ban4"), ("team2", "protect2", "t2_protect2")),
    (("team1", "ban4", "t1_ban4"),),
)
UNSPECIFIED_SEASON_TOKEN = "__unspecified__"
ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

SCRIMS = []
TOURNAMENT_MATCHES = []
NEXT_SCRIM_ID = 1
NEXT_TOURNAMENT_ID = 1
NEXT_MAP_ID = 1
NEXT_EVENT_ID = 1
LAST_SCRIMS_REV = 0
LAST_SCRIMS_ETAG = ""
LAST_STATE_REFRESH_AT = 0.0
MAX_SCRIM_BACKUPS = 100
STATE_REFRESH_INTERVAL_SECONDS = max(0.0, float(os.environ.get("STATE_REFRESH_INTERVAL_SECONDS", "2.0")))


def _connect_db(path=None):
    """Return a SQLite database connection."""
    target = path or DB_PATH
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    return conn


def is_persistent_db_configured() -> bool:
    # Check explicit env vars
    if (os.environ.get("DATABASE_PATH") or "").strip():
        return True
    if (os.environ.get("RENDER_DISK_MOUNT_PATH") or "").strip():
        return True
    # Check if Render disk is auto-mounted at default location
    if (os.environ.get("RENDER") or "").strip().lower() == "true":
        render_default_mount = Path("/var/data")
        if render_default_mount.exists() and os.access(render_default_mount, os.W_OK):
            return True
    return False


def ensure_state_defaults() -> None:
    global SCRIMS, TOURNAMENT_MATCHES, NEXT_SCRIM_ID, NEXT_TOURNAMENT_ID, NEXT_MAP_ID, NEXT_EVENT_ID
    if not isinstance(SCRIMS, list):
        SCRIMS = []
    for scrim in SCRIMS:
        if isinstance(scrim, dict):
            normalize_scrim_record(scrim)
    if not isinstance(TOURNAMENT_MATCHES, list):
        TOURNAMENT_MATCHES = []
    for match in TOURNAMENT_MATCHES:
        if isinstance(match, dict):
            normalize_tournament_record(match)
    NEXT_SCRIM_ID = max(1, int(NEXT_SCRIM_ID or 1))
    NEXT_TOURNAMENT_ID = max(1, int(NEXT_TOURNAMENT_ID or 1))
    NEXT_MAP_ID = max(1, int(NEXT_MAP_ID or 1))
    NEXT_EVENT_ID = max(1, int(NEXT_EVENT_ID or 1))


def _build_scrims_etag(scrims: list) -> str:
    payload = json.dumps(scrims, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = _connect_db()
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect_db()
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                notes TEXT NOT NULL DEFAULT '',
                quality_tag TEXT NOT NULL DEFAULT '',
                logo_path TEXT NOT NULL DEFAULT '',
                is_personal INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                is_sub INTEGER NOT NULL DEFAULT 0,
                main_hero TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, name),
                FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS enemy_teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, name),
                FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS enemy_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                enemy_team_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                is_sub INTEGER NOT NULL DEFAULT 0,
                main_hero TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(enemy_team_id, name),
                FOREIGN KEY (enemy_team_id) REFERENCES enemy_teams(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS app_state (
                state_key TEXT PRIMARY KEY,
                state_value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_state_backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                source TEXT NOT NULL DEFAULT 'save_app_state',
                scrims_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_saved_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL,
                draft_name TEXT NOT NULL,
                season TEXT NOT NULL DEFAULT '',
                draft_slots_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
            );
            """
        )
        for key, default in (
            ("scrims", "[]"),
            ("tournament_matches", "[]"),
            ("next_scrim_id", "1"),
            ("next_tournament_id", "1"),
            ("next_map_id", "1"),
            ("next_event_id", "1"),
            ("scrims_rev", "0"),
            ("site_password_hash", ""),
            ("view_password_hash", ""),
        ):
            conn.execute(
                "INSERT OR IGNORE INTO app_state (state_key, state_value) VALUES (?, ?)",
                (key, default),
            )

        team_columns = {row[1] for row in conn.execute("PRAGMA table_info(teams)").fetchall()}
        if "quality_tag" not in team_columns:
            conn.execute("ALTER TABLE teams ADD COLUMN quality_tag TEXT NOT NULL DEFAULT ''")
        if "logo_path" not in team_columns:
            conn.execute("ALTER TABLE teams ADD COLUMN logo_path TEXT NOT NULL DEFAULT ''")
        if "is_personal" not in team_columns:
            conn.execute("ALTER TABLE teams ADD COLUMN is_personal INTEGER NOT NULL DEFAULT 0")

        player_columns = {row[1] for row in conn.execute("PRAGMA table_info(players)").fetchall()}
        if "is_sub" not in player_columns:
            conn.execute("ALTER TABLE players ADD COLUMN is_sub INTEGER NOT NULL DEFAULT 0")

        enemy_team_columns = {row[1] for row in conn.execute("PRAGMA table_info(enemy_teams)").fetchall()}
        if "logo_path" not in enemy_team_columns:
            conn.execute("ALTER TABLE enemy_teams ADD COLUMN logo_path TEXT NOT NULL DEFAULT ''")

        enemy_player_columns = {row[1] for row in conn.execute("PRAGMA table_info(enemy_players)").fetchall()}
        if "is_sub" not in enemy_player_columns:
            conn.execute("ALTER TABLE enemy_players ADD COLUMN is_sub INTEGER NOT NULL DEFAULT 0")

        TEAM_LOGO_DIR.mkdir(parents=True, exist_ok=True)
        migrate_enemy_teams_to_team_database(conn)
        conn.commit()
    finally:
        conn.close()


def load_app_state() -> None:
    global SCRIMS, TOURNAMENT_MATCHES, NEXT_SCRIM_ID, NEXT_TOURNAMENT_ID, NEXT_MAP_ID, NEXT_EVENT_ID, LAST_SCRIMS_REV, LAST_SCRIMS_ETAG, LAST_STATE_REFRESH_AT
    conn = _connect_db()
    try:
        rows = conn.execute("SELECT state_key, state_value FROM app_state").fetchall()
        state = {row["state_key"]: row["state_value"] for row in rows}

        raw_scrims = json.loads(state.get("scrims", "[]"))
        scrims_changed = False
        normalized_scrims = []
        for scrim in raw_scrims:
            if not isinstance(scrim, dict):
                normalized_scrims.append(scrim)
                continue

            had_season_key = "season" in scrim
            previous_season = scrim.get("season")
            normalize_scrim_record(scrim)
            if not had_season_key or previous_season != scrim.get("season"):
                scrims_changed = True
            normalized_scrims.append(scrim)

        SCRIMS = normalized_scrims
        raw_tournament_matches = json.loads(state.get("tournament_matches", "[]"))
        normalized_tournament_matches = []
        for match in raw_tournament_matches:
            if not isinstance(match, dict):
                normalized_tournament_matches.append(match)
                continue
            normalize_tournament_record(match)
            normalized_tournament_matches.append(match)

        TOURNAMENT_MATCHES = normalized_tournament_matches
        NEXT_SCRIM_ID = int(state.get("next_scrim_id", "1"))
        NEXT_TOURNAMENT_ID = int(state.get("next_tournament_id", "1"))
        NEXT_MAP_ID = int(state.get("next_map_id", "1"))
        NEXT_EVENT_ID = int(state.get("next_event_id", "1"))
        LAST_SCRIMS_REV = int(state.get("scrims_rev", "0"))
        ensure_state_defaults()
        if auto_assign_existing_scrims(SCRIMS):
            scrims_changed = True
        LAST_SCRIMS_ETAG = _build_scrims_etag(SCRIMS)

        if scrims_changed:
            conn.execute(
                "UPDATE app_state SET state_value = ? WHERE state_key = ?",
                (json.dumps(SCRIMS), "scrims"),
            )
            LAST_SCRIMS_REV += 1
            conn.execute(
                "UPDATE app_state SET state_value = ? WHERE state_key = ?",
                (str(LAST_SCRIMS_REV), "scrims_rev"),
            )
            conn.commit()
        LAST_STATE_REFRESH_AT = time.monotonic()
    finally:
        conn.close()


SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "").strip()
EDIT_PASSWORD = os.environ.get("EDIT_PASSWORD", "").strip()
VIEW_PASSWORD = os.environ.get("VIEW_PASSWORD", "").strip()
AUTH_ROLES = {"view", "edit"}

_AUTH_EXEMPT = {"/login", "/logout", "/setup-password"}


def _is_write_request() -> bool:
    return request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}


def _is_edit_session() -> bool:
    return session.get("access_level") == "edit"


def _get_stored_password_hash() -> str:
    row = get_db().execute(
        "SELECT state_value FROM app_state WHERE state_key = ?",
        ("site_password_hash",),
    ).fetchone()
    if not row:
        return ""
    return (row["state_value"] or "").strip()


def _get_stored_view_password_hash() -> str:
    row = get_db().execute(
        "SELECT state_value FROM app_state WHERE state_key = ?",
        ("view_password_hash",),
    ).fetchone()
    if not row:
        return ""
    return (row["state_value"] or "").strip()


def _resolve_edit_password_secret() -> str:
    if EDIT_PASSWORD:
        return EDIT_PASSWORD
    if SITE_PASSWORD:
        return SITE_PASSWORD
    return _get_stored_password_hash()


def _resolve_view_password_secret() -> str:
    if VIEW_PASSWORD:
        return VIEW_PASSWORD
    stored_view_hash = _get_stored_view_password_hash()
    if stored_view_hash:
        return stored_view_hash
    # Backward compatibility: fall back to edit password when dedicated view password is unset.
    return _resolve_edit_password_secret()


def _is_password_configured() -> bool:
    return bool(_resolve_edit_password_secret())


def _current_auth_revision() -> str:
    edit_secret = _resolve_edit_password_secret()
    view_secret = _resolve_view_password_secret()
    if not edit_secret:
        return ""
    raw_value = f"edit:{edit_secret}|view:{view_secret}"
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


def _is_session_authenticated() -> bool:
    if not session.get("logged_in"):
        return False
    if session.get("access_level") not in AUTH_ROLES:
        return False
    return session.get("auth_revision") == _current_auth_revision()


def _mark_session_authenticated(access_level: str) -> None:
    session["logged_in"] = True
    session["access_level"] = access_level
    session["auth_revision"] = _current_auth_revision()


def _clear_auth_session() -> None:
    session.pop("logged_in", None)
    session.pop("access_level", None)
    session.pop("auth_revision", None)


def _normalize_next_path(default: str = "/") -> str:
    next_path = (request.values.get("next") or default).strip()
    if not next_path.startswith("/"):
        return default
    return next_path


@app.before_request
def check_auth() -> None:
    """Require password setup/login before allowing access."""
    if request.path.startswith("/static") or request.path.startswith("/hero-image"):
        return
    if request.path in _AUTH_EXEMPT:
        return
    if not _is_password_configured():
        return redirect(url_for("setup_password", next=request.path))
    if not _is_session_authenticated():
        _clear_auth_session()
        return redirect(url_for("login", next=request.path))
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
    # Keep in-memory state in sync across hosted worker processes without reloading every request.
    if STATE_REFRESH_INTERVAL_SECONDS <= 0:
        load_app_state()
        return
    elapsed = time.monotonic() - LAST_STATE_REFRESH_AT
    if elapsed >= STATE_REFRESH_INTERVAL_SECONDS:
        load_app_state()


def create_manual_db_backup() -> Path:
    backup_dir = DB_PATH.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"rivals_stats_manual_{stamp}.db"

    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    return backup_path


def create_manual_json_dump() -> Path:
    dump_dir = DB_PATH.parent / "backups"
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dump_path = dump_dir / f"rivals_stats_dump_{stamp}.json"

    conn = _connect_db()
    try:
        table_rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

        data = {}
        for table_row in table_rows:
            table_name = table_row["name"]
            escaped_table_name = table_name.replace('"', '""')
            rows = conn.execute(f'SELECT * FROM "{escaped_table_name}"').fetchall()
            data[table_name] = [dict(row) for row in rows]
    finally:
        conn.close()

    # Merge enemy_teams into teams and enemy_players into players so the dump
    # uses unified labels instead of separate "enemy_*" sections.
    for enemy_row in data.pop("enemy_teams", []):
        enemy_row.setdefault("is_enemy", True)
        data.setdefault("teams", []).append(enemy_row)

    for enemy_row in data.pop("enemy_players", []):
        enemy_row.setdefault("is_enemy", True)
        data.setdefault("players", []).append(enemy_row)

    payload = {
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "db_path": str(DB_PATH),
        "table_counts": {table_name: len(rows) for table_name, rows in data.items()},
        "data": data,
    }
    dump_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return dump_path


def save_app_state(*, allow_scrim_removal: bool = False) -> None:
    global SCRIMS, TOURNAMENT_MATCHES, NEXT_SCRIM_ID, NEXT_TOURNAMENT_ID, NEXT_MAP_ID, NEXT_EVENT_ID, LAST_SCRIMS_REV, LAST_SCRIMS_ETAG

    def _safe_json_list(raw_value: str) -> list:
        try:
            parsed = json.loads(raw_value or "[]")
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def _parse_int(raw_value: str, default: int) -> int:
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            return default

    def _merge_scrim_lists(current_scrims: list, pending_scrims: list) -> list:
        merged_by_id = {}
        no_id_scrims = []

        for item in current_scrims:
            if isinstance(item, dict) and isinstance(item.get("id"), int):
                merged_by_id[item["id"]] = item
            elif isinstance(item, dict):
                no_id_scrims.append(item)

        for item in pending_scrims:
            if isinstance(item, dict) and isinstance(item.get("id"), int):
                merged_by_id[item["id"]] = item
            elif isinstance(item, dict):
                no_id_scrims.append(item)

        merged = [merged_by_id[scrim_id] for scrim_id in sorted(merged_by_id.keys())]
        merged.extend(no_id_scrims)
        return merged

    def _recalculate_next_ids(scrims: list, fallback_scrim: int, fallback_map: int, fallback_event: int) -> tuple[int, int, int]:
        max_scrim_id = 0
        max_map_id = 0
        max_event_id = 0

        for scrim in scrims:
            if not isinstance(scrim, dict):
                continue
            scrim_id = scrim.get("id")
            if isinstance(scrim_id, int):
                max_scrim_id = max(max_scrim_id, scrim_id)

            for map_entry in scrim.get("maps", []):
                if not isinstance(map_entry, dict):
                    continue
                map_id = map_entry.get("id")
                if isinstance(map_id, int):
                    max_map_id = max(max_map_id, map_id)

                for event in map_entry.get("events", []):
                    if not isinstance(event, dict):
                        continue
                    event_id = event.get("id")
                    if isinstance(event_id, int):
                        max_event_id = max(max_event_id, event_id)

        return (
            max(fallback_scrim, max_scrim_id + 1),
            max(fallback_map, max_map_id + 1),
            max(fallback_event, max_event_id + 1),
        )

    def _recalculate_next_tournament_id(matches: list, fallback_tournament: int) -> int:
        max_tournament_id = 0
        for match in matches:
            if not isinstance(match, dict):
                continue
            tournament_id = match.get("id")
            if isinstance(tournament_id, int):
                max_tournament_id = max(max_tournament_id, tournament_id)
        return max(fallback_tournament, max_tournament_id + 1)

    def _write_scrim_backup(db_conn: sqlite3.Connection, scrims_to_backup: list, source: str) -> None:
        db_conn.execute(
            "INSERT INTO app_state_backups (source, scrims_json) VALUES (?, ?)",
            (source, json.dumps(scrims_to_backup)),
        )
        db_conn.execute(
            """
            DELETE FROM app_state_backups
            WHERE id NOT IN (
                SELECT id FROM app_state_backups ORDER BY id DESC LIMIT ?
            )
            """,
            (MAX_SCRIM_BACKUPS,),
        )

    ensure_state_defaults()
    for scrim in SCRIMS:
        if isinstance(scrim, dict):
            normalize_scrim_record(scrim)
    for match in TOURNAMENT_MATCHES:
        if isinstance(match, dict):
            normalize_tournament_record(match)

    db = get_db()
    # DDL first (auto-committed by SQLite, doesn't open a Python implicit transaction)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS app_state_backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            source TEXT NOT NULL DEFAULT 'save_app_state',
            scrims_json TEXT NOT NULL
        )
        """
    )
    # Acquire an IMMEDIATE write lock before any DML so Python's sqlite3
    # never opens a weaker implicit deferred transaction that skips this lock.
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    db.execute(
        "INSERT OR IGNORE INTO app_state (state_key, state_value) VALUES (?, ?)",
        ("scrims_rev", "0"),
    )
    rows = db.execute(
        "SELECT state_key, state_value FROM app_state WHERE state_key IN ('scrims', 'tournament_matches', 'scrims_rev', 'next_scrim_id', 'next_tournament_id', 'next_map_id', 'next_event_id')"
    ).fetchall()
    state = {row["state_key"]: row["state_value"] for row in rows}

    persisted_scrims = _safe_json_list(state.get("scrims", "[]"))
    persisted_rev = _parse_int(state.get("scrims_rev", "0"), 0)
    persisted_etag = _build_scrims_etag(persisted_scrims)
    local_etag = LAST_SCRIMS_ETAG or _build_scrims_etag(SCRIMS)

    # Safety rail: unless the caller explicitly allows removals, non-delete
    # operations must never shrink the scrim set.
    if not allow_scrim_removal and len(SCRIMS) < len(persisted_scrims):
        SCRIMS = _merge_scrim_lists(persisted_scrims, SCRIMS)
        ensure_state_defaults()
        if has_request_context():
            flash("Detected a partial scrim state update and auto-restored missing scrims.", "warning")

    conflict_detected = (persisted_rev != LAST_SCRIMS_REV) or (persisted_etag != local_etag)
    if conflict_detected:
        SCRIMS = _merge_scrim_lists(persisted_scrims, SCRIMS)
        ensure_state_defaults()

    persisted_next_scrim = _parse_int(state.get("next_scrim_id", "1"), 1)
    persisted_next_tournament = _parse_int(state.get("next_tournament_id", "1"), 1)
    persisted_next_map = _parse_int(state.get("next_map_id", "1"), 1)
    persisted_next_event = _parse_int(state.get("next_event_id", "1"), 1)
    NEXT_SCRIM_ID, NEXT_MAP_ID, NEXT_EVENT_ID = _recalculate_next_ids(
        SCRIMS,
        max(NEXT_SCRIM_ID, persisted_next_scrim),
        max(NEXT_MAP_ID, persisted_next_map),
        max(NEXT_EVENT_ID, persisted_next_event),
    )
    NEXT_TOURNAMENT_ID = _recalculate_next_tournament_id(
        TOURNAMENT_MATCHES,
        max(NEXT_TOURNAMENT_ID, persisted_next_tournament),
    )

    _write_scrim_backup(db, persisted_scrims, "save_app_state")

    new_rev = persisted_rev + 1
    db.executemany(
        "UPDATE app_state SET state_value = ? WHERE state_key = ?",
        [
            (json.dumps(SCRIMS), "scrims"),
            (json.dumps(TOURNAMENT_MATCHES), "tournament_matches"),
            (str(NEXT_SCRIM_ID), "next_scrim_id"),
            (str(NEXT_TOURNAMENT_ID), "next_tournament_id"),
            (str(NEXT_MAP_ID), "next_map_id"),
            (str(NEXT_EVENT_ID), "next_event_id"),
            (str(new_rev), "scrims_rev"),
        ],
    )
    db.commit()
    LAST_SCRIMS_REV = new_rev
    LAST_SCRIMS_ETAG = _build_scrims_etag(SCRIMS)

    if conflict_detected and has_request_context():
        flash("A concurrent scrim update was detected. Changes were merged to prevent data loss.", "warning")


def _parse_scrim_date(raw_value: str) -> date | None:
    text = (raw_value or "").strip()
    if not text:
        return None

    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _get_season_from_date(scrim_date_str: str) -> str:
    """Determine season from scrim_date based on season windows:
    - Season 7: March 20 and after
    - Season 6.5: Feb 13 to March 19
    - Season 6: Jan 16 to Feb 12
    """
    parsed_date = _parse_scrim_date(scrim_date_str)
    if not parsed_date:
        return ""
    
    # Season 7: March 20 and after
    if parsed_date >= date(2026, 3, 20):
        return "7"
    # Season 6.5: Feb 13 to March 19
    elif parsed_date >= date(2026, 2, 13):
        return "6.5"
    # Season 6: Jan 16 to Feb 12
    elif parsed_date >= date(2026, 1, 16):
        return "6"
    return ""


def normalize_season_value(raw_value: str) -> str:
    return " ".join((raw_value or "").strip().split())


def normalize_match_team_slot(raw_value: str) -> str:
    value = str(raw_value or "").strip()
    return value if value in TEAM_SLOTS else "team1"


def get_scrim_participant_labels(scrim: dict) -> tuple[str, str]:
    team1_name = str(scrim.get("team1_name") or "").strip()
    team2_name = str(scrim.get("team2_name") or "").strip()
    if team1_name or team2_name:
        return team1_name or "This Team", team2_name or "That Team"

    our_label = str(scrim.get("team_name", "")).strip() or "Your Team"
    enemy_label = str(scrim.get("enemy_team") or scrim.get("opponent") or "").strip() or "Enemy Team"
    if normalize_match_team_slot(scrim.get("team_slot", "team1")) == "team2":
        return enemy_label, our_label
    return our_label, enemy_label


def get_scrim_participants(scrim: dict) -> tuple[dict, dict]:
    team1_id = scrim.get("team1_id")
    team2_id = scrim.get("team2_id")
    team1_name, team2_name = get_scrim_participant_labels(scrim)

    if not team1_id and scrim.get("team_id"):
        team1_id = scrim.get("team_id")
    if not team2_id and scrim.get("enemy_team_id"):
        team2_id = scrim.get("enemy_team_id")

    # Canonicalize IDs by name against the teams table to avoid legacy id drift.
    db = get_db()

    def _resolve_team_id_by_name(name: str) -> int | None:
        normalized = (name or "").strip()
        if not normalized:
            return None
        row = db.execute(
            "SELECT id FROM teams WHERE lower(name) = lower(?)",
            (normalized,),
        ).fetchone()
        return int(row["id"]) if row is not None else None

    resolved_team1_id = _resolve_team_id_by_name(team1_name)
    resolved_team2_id = _resolve_team_id_by_name(team2_name)
    if resolved_team1_id:
        team1_id = resolved_team1_id
    if resolved_team2_id:
        team2_id = resolved_team2_id

    # If side labels are different, never keep both sides on the same id.
    if (
        (team1_name or "").strip().lower() != (team2_name or "").strip().lower()
        and team1_id
        and team1_id == team2_id
    ):
        if resolved_team2_id and resolved_team2_id != team1_id:
            team2_id = resolved_team2_id
        elif resolved_team1_id and resolved_team1_id != team2_id:
            team1_id = resolved_team1_id
        else:
            team2_id = None

    return (
        {"id": team1_id, "name": team1_name},
        {"id": team2_id, "name": team2_name},
    )


def get_map_side_default_players(
    match_record: dict,
    map_entry: dict,
    *,
    is_tournament: bool,
    tournament_record: dict | None = None,
) -> dict[str, list[str]]:
    defaults = {"team1": [], "team2": []}

    if is_tournament:
        source = tournament_record if tournament_record is not None else match_record
        team1 = get_tournament_team_by_id(source, map_entry.get("team1_tournament_team_id"))
        team2 = get_tournament_team_by_id(source, map_entry.get("team2_tournament_team_id"))
        defaults["team1"] = build_comp_slot_player_order(
            [{"name": str(name).strip(), "role": ""} for name in (team1 or {}).get("players", []) if str(name).strip()],
            slot_count=6,
        )
        defaults["team2"] = build_comp_slot_player_order(
            [{"name": str(name).strip(), "role": ""} for name in (team2 or {}).get("players", []) if str(name).strip()],
            slot_count=6,
        )
        return defaults

    db = get_db()
    our_team_id = match_record.get("team1_id") or match_record.get("team_id")
    enemy_team_id = match_record.get("team2_id") or match_record.get("enemy_team_id")
    our_team_name = (match_record.get("team1_name") or match_record.get("team_name") or "").strip().lower()
    enemy_team_name = (match_record.get("team2_name") or match_record.get("enemy_team") or match_record.get("opponent") or "").strip().lower()

    def _query_main_team_players(team_id_value: int | None = None, team_name_value: str = "") -> list[dict]:
        rows = []
        if team_id_value:
            rows = db.execute(
                "SELECT name, role FROM players WHERE team_id = ? AND COALESCE(is_sub, 0) = 0 ORDER BY name COLLATE NOCASE",
                (team_id_value,),
            ).fetchall()
        if not rows and (team_name_value or "").strip():
            team_row = db.execute(
                "SELECT id FROM teams WHERE lower(name) = lower(?)",
                ((team_name_value or "").strip(),),
            ).fetchone()
            if team_row:
                rows = db.execute(
                    "SELECT name, role FROM players WHERE team_id = ? AND COALESCE(is_sub, 0) = 0 ORDER BY name COLLATE NOCASE",
                    (team_row["id"],),
                ).fetchall()
        return [
            {"name": (row["name"] or "").strip(), "role": (row["role"] or "").strip()}
            for row in rows
            if (row["name"] or "").strip()
        ]

    def _query_legacy_enemy_players(enemy_team_id_value: int | None) -> list[dict]:
        if not enemy_team_id_value:
            return []
        rows = db.execute(
            "SELECT name, role FROM enemy_players WHERE enemy_team_id = ? ORDER BY name COLLATE NOCASE",
            (enemy_team_id_value,),
        ).fetchall()
        return [
            {"name": (row["name"] or "").strip(), "role": (row["role"] or "").strip()}
            for row in rows
            if (row["name"] or "").strip()
        ]

    def _load_side_player_pool(side_team_id: int | None, side_team_name: str = "") -> list[dict]:
        normalized_side_name = (side_team_name or "").strip().lower()

        if side_team_id and our_team_id and side_team_id == our_team_id:
            return _query_main_team_players(side_team_id)

        if side_team_id and enemy_team_id and side_team_id == enemy_team_id:
            return _query_main_team_players(side_team_id) or _query_legacy_enemy_players(side_team_id)

        if normalized_side_name and our_team_name and normalized_side_name == our_team_name:
            return _query_main_team_players(our_team_id, side_team_name)

        if normalized_side_name and enemy_team_name and normalized_side_name == enemy_team_name:
            return _query_main_team_players(enemy_team_id, side_team_name) or _query_legacy_enemy_players(enemy_team_id)

        # Fallback only when side identity is unknown.
        if side_team_id:
            return _query_main_team_players(side_team_id) or _query_legacy_enemy_players(side_team_id)

        normalized_name = (side_team_name or "").strip()
        if normalized_name:
            return _query_main_team_players(None, normalized_name)

        return []

    for side in TEAM_SLOTS:
        side_team_id = map_entry.get(f"{side}_id")
        side_team_name = map_entry.get(f"{side}_name", "")
        player_rows = _load_side_player_pool(side_team_id, side_team_name)
        defaults[side] = build_comp_slot_player_order(
            player_rows,
            slot_count=6,
        )

    return defaults


def _build_player_hero_pair_history(
    team_id: int | None,
    team_name: str,
    candidate_players: list[str],
) -> tuple[dict[tuple[str, str], dict[str, int]], dict[str, int]]:
    pair_counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"count": 0, "wins": 0})
    player_totals: dict[str, int] = defaultdict(int)

    candidate_set = {name for name in candidate_players if name}
    if not candidate_set:
        return pair_counts, player_totals

    team_scrims = get_scrims_for_team(team_id, team_name)
    for scrim in team_scrims:
        for map_entry in scrim.get("maps", []):
            if not isinstance(map_entry, dict):
                continue

            our_team_slot = normalize_match_team_slot(map_entry.get("our_team_slot", "team1"))
            outcome = get_map_outcome_for_slot(map_entry, our_team_slot)

            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(our_team_slot, []):
                    if not isinstance(slot, dict):
                        continue
                    player_name = (slot.get("player") or "").strip()
                    if not player_name or player_name not in candidate_set:
                        continue
                    hero_name = canonicalize_hero_name(slot.get("hero", ""))
                    if not hero_name:
                        continue

                    pair_counts[(hero_name, player_name)]["count"] += 1
                    if outcome == "Win":
                        pair_counts[(hero_name, player_name)]["wins"] += 1
                    player_totals[player_name] += 1

    return pair_counts, player_totals


def _auto_assign_players_to_heroes_for_side(
    team_slots: list[dict],
    candidate_players: list[str],
    *,
    team_id: int | None,
    team_name: str,
) -> list[dict]:
    cleaned_candidates = []
    seen = set()
    for name in candidate_players:
        player_name = (name or "").strip()
        if player_name and player_name not in seen:
            seen.add(player_name)
            cleaned_candidates.append(player_name)

    if not team_slots or not cleaned_candidates:
        return team_slots

    pair_counts, player_totals = _build_player_hero_pair_history(team_id, team_name, cleaned_candidates)
    candidate_index = {name: idx for idx, name in enumerate(cleaned_candidates)}
    used_players: set[str] = set()

    # Preserve explicitly assigned players and avoid reassigning them.
    for slot in team_slots:
        if not isinstance(slot, dict):
            continue
        existing_player = (slot.get("player") or "").strip()
        if existing_player:
            used_players.add(existing_player)

    hero_slot_indices = []
    for idx, slot in enumerate(team_slots):
        if not isinstance(slot, dict):
            continue
        if canonicalize_hero_name(slot.get("hero", "")):
            hero_slot_indices.append(idx)

    for idx in hero_slot_indices:
        slot = team_slots[idx]
        current_player = (slot.get("player") or "").strip()
        if current_player:
            continue
        hero_name = canonicalize_hero_name(slot.get("hero", ""))
        if not hero_name:
            continue

        best_name = None
        best_score = None
        for player_name in cleaned_candidates:
            if player_name in used_players:
                continue

            stats = pair_counts.get((hero_name, player_name), {"count": 0, "wins": 0})
            score = (
                stats.get("count", 0),
                stats.get("wins", 0),
                player_totals.get(player_name, 0),
                -candidate_index.get(player_name, 999),
            )
            if best_score is None or score > best_score:
                best_score = score
                best_name = player_name

        if best_name:
            slot["player"] = best_name
            used_players.add(best_name)

    for slot in team_slots:
        if not isinstance(slot, dict):
            continue
        if (slot.get("player") or "").strip():
            continue
        next_name = next((name for name in cleaned_candidates if name not in used_players), None)
        if next_name:
            slot["player"] = next_name
            used_players.add(next_name)

    return team_slots


def auto_assign_section_players_from_heroes(
    match_record: dict,
    map_entry: dict,
    section: dict,
    *,
    is_tournament: bool,
    tournament_record: dict | None = None,
) -> None:
    defaults = get_map_side_default_players(
        match_record,
        map_entry,
        is_tournament=is_tournament,
        tournament_record=tournament_record,
    )

    for side in TEAM_SLOTS:
        side_slots = section.get(side, [])
        if not isinstance(side_slots, list):
            continue
        section[side] = _auto_assign_players_to_heroes_for_side(
            side_slots,
            defaults.get(side, []),
            team_id=map_entry.get(f"{side}_id"),
            team_name=(map_entry.get(f"{side}_name") or "").strip(),
        )


def scrim_involves_team(scrim: dict, team_id: int | None, team_name: str = "") -> bool:
    if team_id is not None and (
        scrim.get("team1_id") == team_id
        or scrim.get("team2_id") == team_id
        or scrim.get("team_id") == team_id
    ):
        return True

    team_name_lower = (team_name or "").strip().lower()
    if not team_name_lower:
        return False

    participant_names = [
        (scrim.get("team_name", "") or "").strip().lower(),
        (scrim.get("team1_name", "") or "").strip().lower(),
        (scrim.get("team2_name", "") or "").strip().lower(),
    ]
    return any(name == team_name_lower for name in participant_names if name)


def get_scrims_for_team(team_id: int | None, team_name: str = "") -> list[dict]:
    relevant_scrims = [scrim for scrim in SCRIMS if scrim_involves_team(scrim, team_id, team_name)]
    remapped_scrims: list[dict] = []

    team_name_lower = (team_name or "").strip().lower()

    def _normalize_player_keys(raw_names: list[str]) -> set[str]:
        keys: set[str] = set()
        for raw_name in raw_names or []:
            player_name = normalize_player_name(raw_name)
            key = _compact_text(player_name)
            if key:
                keys.add(key)
        return keys

    selected_team_player_keys: set[str] = set()
    if team_id is not None or team_name_lower:
        roster_db = get_db() if has_request_context() else _connect_db()
        try:
            resolved_team_id = team_id
            if resolved_team_id is None and team_name_lower:
                team_row = roster_db.execute(
                    "SELECT id FROM teams WHERE lower(name) = lower(?)",
                    (team_name,),
                ).fetchone()
                if team_row is not None:
                    resolved_team_id = int(team_row["id"])
            if resolved_team_id is not None:
                selected_team_player_keys = _normalize_player_keys(
                    [
                        row["name"]
                        for row in roster_db.execute(
                            "SELECT name FROM players WHERE team_id = ? AND COALESCE(is_sub, 0) = 0",
                            (resolved_team_id,),
                        ).fetchall()
                    ]
                )
        finally:
            if not has_request_context():
                roster_db.close()

    def _resolve_slot_for_record(record: dict, fallback_slot: str | None = None) -> str:
        if team_id is not None:
            if record.get("team1_id") == team_id:
                return "team1"
            if record.get("team2_id") == team_id:
                return "team2"

        if team_name_lower:
            if str(record.get("team1_name", "")).strip().lower() == team_name_lower:
                return "team1"
            if str(record.get("team2_name", "")).strip().lower() == team_name_lower:
                return "team2"

        return normalize_match_team_slot(fallback_slot or record.get("team_slot", "team1"))

    def _map_side_player_keys(map_record: dict, side: str) -> set[str]:
        keys: set[str] = set()
        for section in map_record.get("comp", []):
            if not isinstance(section, dict):
                continue
            for slot in section.get(side, []):
                if not isinstance(slot, dict):
                    continue
                player_name = normalize_player_name(slot.get("player", ""))
                key = _compact_text(player_name)
                if key:
                    keys.add(key)
        return keys

    def _resolve_map_team_slot(scrim_record: dict, map_record: dict, scrim_slot: str) -> str:
        stored_map_slot = (map_record.get("our_team_slot") or "").strip()
        if (
            team_id is not None
            and scrim_record.get("team_id") == team_id
            and stored_map_slot in TEAM_SLOTS
        ):
            map_team_slot = stored_map_slot
        else:
            map_team_slot = _resolve_slot_for_record(map_record, scrim_slot)

        roster_candidates: list[set[str]] = []
        scrim_side_player_keys = _normalize_player_keys(scrim_record.get(f"{scrim_slot}_players", []))
        if scrim_side_player_keys:
            roster_candidates.append(scrim_side_player_keys)
        if selected_team_player_keys:
            roster_candidates.append(selected_team_player_keys)
        if not roster_candidates:
            return map_team_slot

        current_side_keys = _map_side_player_keys(map_record, map_team_slot)
        other_team_slot = opposite_team_slot(map_team_slot)
        other_side_keys = _map_side_player_keys(map_record, other_team_slot)
        if not current_side_keys and not other_side_keys:
            return map_team_slot

        current_score = max(len(current_side_keys & roster_keys) for roster_keys in roster_candidates)
        other_score = max(len(other_side_keys & roster_keys) for roster_keys in roster_candidates)
        return other_team_slot if other_score > current_score else map_team_slot

    for original_scrim in relevant_scrims:
        scrim = copy.deepcopy(original_scrim)
        team_slot = _resolve_slot_for_record(scrim)

        remapped_maps: list[dict] = []
        for original_map in scrim.get("maps", []):
            if not isinstance(original_map, dict):
                continue
            map_entry = copy.deepcopy(original_map)
            map_team_slot = _resolve_map_team_slot(original_scrim, original_map, team_slot)
            map_entry["our_team_slot"] = map_team_slot
            map_entry["result"] = get_result_for_slot(original_map, map_team_slot)
            inferred_result = infer_result_from_score_text(map_entry.get("score", ""), slot=map_team_slot)
            if inferred_result:
                map_entry["result"] = inferred_result
            remapped_maps.append(map_entry)

        scrim["team_slot"] = team_slot
        scrim["maps"] = remapped_maps
        remapped_scrims.append(scrim)

    return remapped_scrims


def normalize_scrim_record(scrim: dict) -> dict:
    # Auto-assign season from scrim_date if season is empty
    season = normalize_season_value(scrim.get("season", ""))
    if not season:
        season = _get_season_from_date(scrim.get("scrim_date", ""))
    scrim["season"] = season
    scrim["team_slot"] = normalize_match_team_slot(scrim.get("team_slot", "team1"))
    if not scrim.get("enemy_team") and scrim.get("opponent"):
        scrim["enemy_team"] = scrim.get("opponent", "")
    if not scrim.get("opponent") and scrim.get("enemy_team"):
        scrim["opponent"] = scrim.get("enemy_team", "")
    scrim.setdefault("team1_id", scrim.get("team_id"))
    scrim.setdefault("team2_id", scrim.get("enemy_team_id"))
    scrim["team1_players"] = parse_name_list("\n".join(scrim.get("team1_players", [])))
    scrim["team2_players"] = parse_name_list("\n".join(scrim.get("team2_players", [])))
    if not scrim.get("team1_name"):
        scrim["team1_name"] = str(scrim.get("team_name", "")).strip()
    if not scrim.get("team2_name"):
        scrim["team2_name"] = str(scrim.get("enemy_team") or scrim.get("opponent") or "").strip()
    for map_entry in scrim.get("maps", []):
        if not isinstance(map_entry, dict):
            continue
        draft = map_entry.get("draft", {})
        if isinstance(draft, dict):
            for side in TEAM_SLOTS:
                team_draft = draft.get(side, {})
                if not isinstance(team_draft, dict):
                    continue
                for slot_key, hero_name in list(team_draft.items()):
                    team_draft[slot_key] = normalize_hero_slot_value(hero_name)

        comp_sections = map_entry.get("comp", [])
        if isinstance(comp_sections, list):
            for section in comp_sections:
                if not isinstance(section, dict):
                    continue
                for side in TEAM_SLOTS:
                    slots = section.get(side, [])
                    if not isinstance(slots, list):
                        continue
                    for slot in slots:
                        if not isinstance(slot, dict):
                            continue
                        raw_player_name = str(slot.get("player", "")).strip()
                        if is_ringer_player_name(raw_player_name):
                            slot["player"] = ""
                        else:
                            slot["player"] = normalize_player_name(raw_player_name)
                        slot["hero"] = normalize_hero_slot_value(slot.get("hero", ""))
    return scrim


def normalize_tournament_record(match: dict) -> dict:
    match["season"] = normalize_season_value(match.get("season", ""))
    match["team_slot"] = normalize_match_team_slot(match.get("team_slot", "team1"))
    match.setdefault("notes", "")
    match.setdefault("maps", [])
    match.setdefault("matches", [])
    match.setdefault("team_id", None)
    match.setdefault("team_name", "")
    match.setdefault("tournament_name", "")
    match.setdefault("tournament_teams", [])
    match.setdefault("team1_enemy_id", None)
    match.setdefault("team1_name", "")
    match.setdefault("team1_players", [])
    match.setdefault("team2_enemy_id", None)
    match.setdefault("team2_name", "")
    match.setdefault("team2_players", [])
    match["team1_players"] = parse_name_list("\n".join(str(player) for player in match.get("team1_players", [])))
    match["team2_players"] = parse_name_list("\n".join(str(player) for player in match.get("team2_players", [])))

    normalized_teams: list[dict] = []
    next_team_id = 1
    for team in match.get("tournament_teams", []):
        if not isinstance(team, dict):
            continue
        name = str(team.get("name", "")).strip()
        if not name:
            continue
        raw_id = team.get("id")
        team_id = raw_id if isinstance(raw_id, int) and raw_id > 0 else next_team_id
        next_team_id = max(next_team_id, team_id + 1)
        players = parse_name_list("\n".join(str(player) for player in team.get("players", [])))
        normalized_teams.append({
            "id": team_id,
            "name": name,
            "players": players,
        })

    if not normalized_teams:
        if match.get("team1_name"):
            normalized_teams.append({
                "id": next_team_id,
                "name": str(match.get("team1_name", "")).strip(),
                "players": list(match.get("team1_players", [])),
            })
            next_team_id += 1
        if match.get("team2_name") and str(match.get("team2_name", "")).strip().lower() != str(match.get("team1_name", "")).strip().lower():
            normalized_teams.append({
                "id": next_team_id,
                "name": str(match.get("team2_name", "")).strip(),
                "players": list(match.get("team2_players", [])),
            })

    match["tournament_teams"] = normalized_teams

    normalized_matches: list[dict] = []
    next_match_id = 1
    for tournament_match in match.get("matches", []):
        if not isinstance(tournament_match, dict):
            continue
        raw_id = tournament_match.get("id")
        tournament_match_id = raw_id if isinstance(raw_id, int) and raw_id > 0 else next_match_id
        next_match_id = max(next_match_id, tournament_match_id + 1)
        tournament_match["id"] = tournament_match_id
        normalized_matches.append(normalize_tournament_match_record(tournament_match, normalized_teams))

    if not normalized_matches and match.get("maps"):
        grouped_matches: dict[tuple, dict] = {}
        for map_entry in match.get("maps", []):
            if not isinstance(map_entry, dict):
                continue

            team1_name = str(map_entry.get("team1_name", "")).strip() or str(match.get("team1_name", "")).strip() or "Team 1"
            team2_name = str(map_entry.get("team2_name", "")).strip() or str(match.get("team2_name", "")).strip() or "Team 2"
            team1_id = map_entry.get("team1_tournament_team_id") if isinstance(map_entry.get("team1_tournament_team_id"), int) else None
            team2_id = map_entry.get("team2_tournament_team_id") if isinstance(map_entry.get("team2_tournament_team_id"), int) else None
            match_key = (
                team1_id or 0,
                team1_name.lower(),
                team2_id or 0,
                team2_name.lower(),
            )
            generated_match = grouped_matches.get(match_key)
            if generated_match is None:
                generated_match = {
                    "id": next_match_id,
                    "scrim_date": match.get("scrim_date", ""),
                    "notes": "",
                    "team1_tournament_team_id": team1_id,
                    "team2_tournament_team_id": team2_id,
                    "team1_name": team1_name,
                    "team2_name": team2_name,
                    "maps": [],
                }
                grouped_matches[match_key] = generated_match
                next_match_id += 1
            generated_match["maps"].append(map_entry)
        normalized_matches = [
            normalize_tournament_match_record(tournament_match, normalized_teams)
            for tournament_match in grouped_matches.values()
        ]

    match["matches"] = normalized_matches
    match["maps"] = []

    return match


def _scrim_side_team_key(scrim: dict, side: str) -> str:
    side_id = scrim.get(f"{side}_id")
    if side_id:
        return f"id:{side_id}"
    side_name = (scrim.get(f"{side}_name") or "").strip().lower()
    return f"name:{side_name}"


def _build_existing_scrim_pair_history(scrims: list[dict]) -> dict[str, dict[tuple[str, str], int]]:
    history: dict[str, dict[tuple[str, str], int]] = defaultdict(lambda: defaultdict(int))

    for scrim in scrims:
        if not isinstance(scrim, dict):
            continue
        for map_entry in scrim.get("maps", []):
            if not isinstance(map_entry, dict):
                continue
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for side in TEAM_SLOTS:
                    team_key = _scrim_side_team_key(scrim, side)
                    if team_key.endswith("name:"):
                        continue
                    for slot in section.get(side, []):
                        if not isinstance(slot, dict):
                            continue
                        hero_name = canonicalize_hero_name(slot.get("hero", ""))
                        player_name = (slot.get("player") or "").strip()
                        if not hero_name or not player_name:
                            continue
                        history[team_key][(hero_name, player_name)] += 1

    return history


def auto_assign_existing_scrims(scrims: list[dict]) -> bool:
    history = _build_existing_scrim_pair_history(scrims)
    changed = False

    for scrim in scrims:
        if not isinstance(scrim, dict):
            continue

        roster_by_side = {
            "team1": [str(name).strip() for name in scrim.get("team1_players", []) if str(name).strip()],
            "team2": [str(name).strip() for name in scrim.get("team2_players", []) if str(name).strip()],
        }

        for map_entry in scrim.get("maps", []):
            if not isinstance(map_entry, dict):
                continue
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue

                for side in TEAM_SLOTS:
                    slots = section.get(side, [])
                    if not isinstance(slots, list):
                        continue

                    roster = roster_by_side.get(side, [])
                    if not roster:
                        continue

                    team_key = _scrim_side_team_key(scrim, side)
                    pair_counts = history.get(team_key, {})
                    used_players: set[str] = set()

                    for slot in slots:
                        if not isinstance(slot, dict):
                            continue
                        hero_name = canonicalize_hero_name(slot.get("hero", ""))
                        if not hero_name:
                            continue

                        best_player = None
                        best_count = -1
                        for player_name in roster:
                            if player_name in used_players:
                                continue
                            count = pair_counts.get((hero_name, player_name), 0)
                            if count > best_count:
                                best_count = count
                                best_player = player_name

                        if not best_player:
                            best_player = next((name for name in roster if name not in used_players), None)

                        if best_player and (slot.get("player") or "").strip() != best_player:
                            slot["player"] = best_player
                            changed = True
                        if best_player:
                            used_players.add(best_player)

    return changed


def normalize_tournament_match_record(tournament_match: dict, tournament_teams: list[dict]) -> dict:
    tournament_match.setdefault("notes", "")
    tournament_match.setdefault("maps", [])
    tournament_match.setdefault("scrim_date", "")
    tournament_match.setdefault("team1_tournament_team_id", None)
    tournament_match.setdefault("team2_tournament_team_id", None)
    tournament_match.setdefault("team1_name", "")
    tournament_match.setdefault("team2_name", "")

    team1 = find_tournament_team_by_id(tournament_teams, tournament_match.get("team1_tournament_team_id"))
    team2 = find_tournament_team_by_id(tournament_teams, tournament_match.get("team2_tournament_team_id"))

    if team1 is not None:
        tournament_match["team1_name"] = team1.get("name", "")
    elif not tournament_match.get("team1_tournament_team_id") and tournament_match.get("team1_name"):
        inferred_team = find_tournament_team_by_name(tournament_teams, tournament_match.get("team1_name", ""))
        if inferred_team is not None:
            tournament_match["team1_tournament_team_id"] = inferred_team["id"]
            tournament_match["team1_name"] = inferred_team["name"]

    if team2 is not None:
        tournament_match["team2_name"] = team2.get("name", "")
    elif not tournament_match.get("team2_tournament_team_id") and tournament_match.get("team2_name"):
        inferred_team = find_tournament_team_by_name(tournament_teams, tournament_match.get("team2_name", ""))
        if inferred_team is not None:
            tournament_match["team2_tournament_team_id"] = inferred_team["id"]
            tournament_match["team2_name"] = inferred_team["name"]

    for map_entry in tournament_match.get("maps", []):
        if not isinstance(map_entry, dict):
            continue
        map_entry.setdefault("team1_tournament_team_id", tournament_match.get("team1_tournament_team_id"))
        map_entry.setdefault("team2_tournament_team_id", tournament_match.get("team2_tournament_team_id"))
        map_entry.setdefault("team1_name", tournament_match.get("team1_name", ""))
        map_entry.setdefault("team2_name", tournament_match.get("team2_name", ""))
        map_entry.setdefault("picked_by_tournament_team_id", None)
        map_entry.setdefault("picked_by_name", "")

        team1_map_team = find_tournament_team_by_id(tournament_teams, map_entry.get("team1_tournament_team_id"))
        team2_map_team = find_tournament_team_by_id(tournament_teams, map_entry.get("team2_tournament_team_id"))
        if team1_map_team is not None:
            map_entry["team1_name"] = team1_map_team["name"]
        if team2_map_team is not None:
            map_entry["team2_name"] = team2_map_team["name"]

        if map_entry.get("picked_by_tournament_team_id") is None and map_entry.get("picked_by_name"):
            picker = find_tournament_team_by_name(tournament_teams, map_entry.get("picked_by_name", ""))
            if picker is not None:
                map_entry["picked_by_tournament_team_id"] = picker["id"]
        picker = find_tournament_team_by_id(tournament_teams, map_entry.get("picked_by_tournament_team_id"))
        map_entry["picked_by_name"] = picker.get("name", "") if picker is not None else str(map_entry.get("picked_by_name", "")).strip()

        draft = map_entry.get("draft", {})
        if isinstance(draft, dict):
            for side in TEAM_SLOTS:
                team_draft = draft.get(side, {})
                if not isinstance(team_draft, dict):
                    continue
                for slot_key, hero_name in list(team_draft.items()):
                    team_draft[slot_key] = normalize_hero_slot_value(hero_name)

        comp_sections = map_entry.get("comp", [])
        if isinstance(comp_sections, list):
            for section in comp_sections:
                if not isinstance(section, dict):
                    continue
                for side in TEAM_SLOTS:
                    slots = section.get(side, [])
                    if not isinstance(slots, list):
                        continue
                    for slot in slots:
                        if not isinstance(slot, dict):
                            continue
                        raw_player_name = str(slot.get("player", "")).strip()
                        if is_ringer_player_name(raw_player_name):
                            slot["player"] = ""
                        else:
                            slot["player"] = normalize_player_name(raw_player_name)
                        slot["hero"] = normalize_hero_slot_value(slot.get("hero", ""))

    return tournament_match


def get_scrim_season_options(scrims: list[dict]) -> list[str]:
    seasons = {
        normalize_season_value(scrim.get("season", ""))
        for scrim in scrims
        if normalize_season_value(scrim.get("season", ""))
    }
    return sorted(seasons, key=lambda value: [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)])


def get_current_season_from_recent_scrim(scrims: list[dict]) -> str:
    for scrim in reversed(scrims):
        season = normalize_season_value(scrim.get("season", ""))
        if season:
            return season
    return "all"


def get_selected_season(
    raw_value: str,
    season_options: list[str],
    *,
    allow_unspecified: bool = False,
    default_season: str = "all",
) -> str:
    selected = normalize_season_value(raw_value)
    if not selected or selected.lower() == "all":
        normalized_default = normalize_season_value(default_season)
        if normalized_default == UNSPECIFIED_SEASON_TOKEN and allow_unspecified:
            return UNSPECIFIED_SEASON_TOKEN
        if normalized_default in season_options:
            return normalized_default
        return "all"
    if selected == UNSPECIFIED_SEASON_TOKEN and allow_unspecified:
        return UNSPECIFIED_SEASON_TOKEN
    return selected if selected in season_options else "all"


def filter_scrims_by_season(scrims: list[dict], season: str) -> list[dict]:
    selected = normalize_season_value(season)
    if not selected or selected.lower() == "all":
        return scrims
    if selected == UNSPECIFIED_SEASON_TOKEN:
        return [scrim for scrim in scrims if not normalize_season_value(scrim.get("season", ""))]
    return [scrim for scrim in scrims if normalize_season_value(scrim.get("season", "")) == selected]


def get_selected_map_type(raw_value: str) -> str:
    raw = (raw_value or "").strip()
    if not raw or raw.lower() == "all":
        return "all"

    normalized = normalize_map_type_value(raw)
    return normalized if normalized in MAP_TYPES else "all"


def filter_scrims_by_map_type(scrims: list[dict], selected_map_type: str) -> list[dict]:
    selected = get_selected_map_type(selected_map_type)
    if selected == "all":
        return scrims

    filtered_scrims: list[dict] = []
    for scrim in scrims:
        if not isinstance(scrim, dict):
            continue
        filtered_maps = [
            map_entry
            for map_entry in scrim.get("maps", [])
            if isinstance(map_entry, dict)
            and normalize_map_type_value(map_entry.get("map_type", "")) == selected
        ]
        if not filtered_maps:
            continue
        filtered_scrim = dict(scrim)
        filtered_scrim["maps"] = filtered_maps
        filtered_scrims.append(filtered_scrim)

    return filtered_scrims


def compute_player_stats(player_name: str, scrims: list[dict] | None = None) -> dict:
    target = player_name.strip()
    if not target:
        return {
            "maps_played": 0,
            "wins": 0,
            "losses": 0,
            "events_mentioned": 0,
            "win_rate": 0,
        }

    maps_played = 0
    wins = 0
    losses = 0
    unresolved_maps = 0
    events_mentioned = 0
    target_lower = target.lower()
    exact_name_pattern = re.compile(r"(?<!\\w)" + re.escape(target_lower) + r"(?!\\w)")
    source_scrims = scrims if scrims is not None else SCRIMS

    for scrim in source_scrims:
        for map_entry in scrim["maps"]:
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            player_found = False
            for section in map_entry.get("comp", []):
                for slot in section.get(our_team_slot, []):
                    if slot.get("player", "").strip().lower() == target_lower:
                        player_found = True
                        break
                if player_found:
                    break

            if player_found:
                maps_played += 1
                result = get_map_outcome_for_slot(map_entry, our_team_slot)
                if result == "Win":
                    wins += 1
                elif result == "Loss":
                    losses += 1
                else:
                    unresolved_maps += 1

            for event in map_entry.get("events", []):
                description = event.get("description", "").strip().lower()
                if exact_name_pattern.search(description):
                    events_mentioned += 1

    decided_maps = wins + losses
    win_rate = round((wins / decided_maps) * 100, 1) if decided_maps else 0

    return {
        "maps_played": maps_played,
        "decided_maps": decided_maps,
        "unresolved_maps": unresolved_maps,
        "wins": wins,
        "losses": losses,
        "events_mentioned": events_mentioned,
        "win_rate": win_rate,
    }


def build_player_recent_maps(player_name: str, scrims: list[dict], *, limit: int = 15) -> list[dict]:
    target_name = (player_name or "").strip().lower()
    if not target_name:
        return []

    rows: list[dict] = []
    for scrim in scrims:
        scrim_date = (scrim.get("scrim_date") or "").strip()
        # enemy_team/opponent always identifies the actual opponent regardless of
        # which team slot our team occupies, so use it directly.
        opponent_name = str(scrim.get("enemy_team") or scrim.get("opponent") or "").strip() or "Enemy Team"

        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            heroes: set[str] = set()
            found = False
            for section in map_entry.get("comp", []):
                for slot in section.get(our_team_slot, []):
                    if (slot.get("player", "") or "").strip().lower() != target_name:
                        continue
                    found = True
                    hero_name = _canonical_draft_hero(slot.get("hero", ""))
                    if hero_name:
                        heroes.add(hero_name)

            if not found:
                continue

            rows.append(
                {
                    "scrim_id": scrim.get("id"),
                    "scrim_date": scrim_date,
                    "map_name": (map_entry.get("map_name") or "").strip(),
                    "result": get_map_outcome_for_slot(map_entry, our_team_slot),
                    "opponent": opponent_name,
                    "heroes": sorted(heroes),
                }
            )

    rows.sort(
        key=lambda row: (
            row.get("scrim_date", ""),
            int(row.get("scrim_id") or 0),
        ),
        reverse=True,
    )
    return rows[: max(1, int(limit or 1))]


def build_player_submap_swap_summary(player_name: str, scrims: list[dict], *, limit: int = 20) -> dict:
    target_name = (player_name or "").strip().lower()
    if not target_name:
        return {"swap_count": 0, "transition_count": 0, "swap_rate": 0, "swap_events": []}

    transitions = 0
    swaps = 0
    swap_events: list[dict] = []

    for scrim in scrims:
        scrim_date = (scrim.get("scrim_date") or "").strip()
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            section_rows: list[dict] = []
            for idx, section in enumerate(map_entry.get("comp", [])):
                heroes: set[str] = set()
                for slot in section.get(our_team_slot, []):
                    if (slot.get("player", "") or "").strip().lower() != target_name:
                        continue
                    hero_name = _canonical_draft_hero(slot.get("hero", ""))
                    if hero_name:
                        heroes.add(hero_name)
                if not heroes:
                    continue
                section_rows.append(
                    {
                        "label": (section.get("submap") or f"Round {idx + 1}").strip(),
                        "heroes": sorted(heroes),
                    }
                )

            if len(section_rows) < 2:
                continue

            for prev, curr in zip(section_rows, section_rows[1:]):
                transitions += 1
                if prev["heroes"] == curr["heroes"]:
                    continue
                swaps += 1
                swap_events.append(
                    {
                        "scrim_id": scrim.get("id"),
                        "scrim_date": scrim_date,
                        "map_name": (map_entry.get("map_name") or "").strip(),
                        "from_label": prev["label"],
                        "to_label": curr["label"],
                        "from_heroes": prev["heroes"],
                        "to_heroes": curr["heroes"],
                    }
                )

    swap_events.sort(
        key=lambda row: (
            row.get("scrim_date", ""),
            int(row.get("scrim_id") or 0),
        ),
        reverse=True,
    )

    return {
        "swap_count": swaps,
        "transition_count": transitions,
        "swap_rate": round((swaps / transitions) * 100, 1) if transitions else 0,
        "swap_events": swap_events[: max(1, int(limit or 1))],
    }


def build_player_hero_map_breakdown(player_name: str, scrims: list[dict]) -> dict:
    target_name = (player_name or "").strip().lower()
    if not target_name:
        return {
            "hero_rows": [],
            "map_rows": [],
        }

    hero_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    map_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            sections = map_entry.get("comp", [])

            # Count played submaps (sections with a named submap that have hero data)
            # to compute per-submap weight (1/N). Sections without a submap name are
            # rounds worth 0.5 each.
            played_submaps = sum(
                1 for s in sections
                if s.get("submap") and any(
                    (sl.get("hero") or "").strip()
                    for sl in s.get("team1", []) + s.get("team2", [])
                )
            )

            player_found = False
            hero_weights: dict[str, float] = {}
            for section in sections:
                is_submap = bool(section.get("submap"))
                if is_submap:
                    weight = 1.0 / played_submaps if played_submaps > 0 else 0.5
                else:
                    weight = 0.5
                for slot in section.get(our_team_slot, []):
                    if (slot.get("player", "") or "").strip().lower() != target_name:
                        continue
                    player_found = True
                    hero_name = _canonical_draft_hero(slot.get("hero", ""))
                    if hero_name:
                        hero_weights[hero_name] = hero_weights.get(hero_name, 0.0) + weight

            if not player_found:
                continue

            map_name = (map_entry.get("map_name", "") or "").strip()
            result = get_map_outcome_for_slot(map_entry, our_team_slot)

            if map_name:
                map_stats[map_name]["maps"] += 1
                if result == "Win":
                    map_stats[map_name]["wins"] += 1
                    map_stats[map_name]["decided"] += 1
                elif result == "Loss":
                    map_stats[map_name]["losses"] += 1
                    map_stats[map_name]["decided"] += 1
                else:
                    map_stats[map_name]["unresolved"] += 1

            for hero_name, w in hero_weights.items():
                hero_stats[hero_name]["maps"] += w
                if result == "Win":
                    hero_stats[hero_name]["wins"] += w
                    hero_stats[hero_name]["decided"] += w
                elif result == "Loss":
                    hero_stats[hero_name]["losses"] += w
                    hero_stats[hero_name]["decided"] += w
                else:
                    hero_stats[hero_name]["unresolved"] += w

    hero_rows = []
    for hero_name, stats in hero_stats.items():
        maps_played = round(stats["maps"], 2)
        decided_maps = round(stats["decided"], 2)
        hero_rows.append(
            {
                "hero": hero_name,
                "maps": maps_played,
                "decided_maps": decided_maps,
                "unresolved_maps": stats["unresolved"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round((stats["wins"] / decided_maps) * 100, 1) if decided_maps else 0,
            }
        )
    hero_rows.sort(key=lambda row: (row["maps"], row["win_rate"]), reverse=True)

    map_rows = []
    for map_name, stats in map_stats.items():
        maps_played = stats["maps"]
        decided_maps = stats["decided"]
        map_rows.append(
            {
                "map_name": map_name,
                "mode": MAP_MODES.get(map_name, "Other"),
                "maps": maps_played,
                "decided_maps": decided_maps,
                "unresolved_maps": stats["unresolved"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round((stats["wins"] / decided_maps) * 100, 1) if decided_maps else 0,
                "image": MAP_IMAGES.get(map_name, ""),
            }
        )
    map_rows.sort(key=lambda row: (row["maps"], row["win_rate"]), reverse=True)

    return {
        "hero_rows": hero_rows,
        "map_rows": map_rows,
    }


def build_player_ban_impact(player_name: str, scrims: list[dict]) -> list[dict]:
    """Return ban impact rows for every hero in the player's pool.

    Each row contains:
      hero, hero_maps, hero_wr, times_banned, wr_when_banned, wr_delta, all_pivots, top_pivot
    """
    target_name = (player_name or "").strip().lower()
    if not target_name:
        return []

    # First pass: count how many maps the player appeared on each hero.
    hero_total_maps: dict[str, int] = defaultdict(int)
    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            seen_heroes: set[str] = set()
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(our_team_slot, []):
                    if (slot.get("player", "") or "").strip().lower() != target_name:
                        continue
                    h = _canonical_draft_hero(slot.get("hero", ""))
                    if h and h not in seen_heroes:
                        hero_total_maps[h] += 1
                        seen_heroes.add(h)

    if not hero_total_maps:
        return []

    # Track heroes with enough play time to be meaningful (≥5 maps), sorted by most played.
    all_heroes = sorted(
        (h for h in hero_total_maps if hero_total_maps[h] >= 5),
        key=lambda h: hero_total_maps[h], reverse=True,
    )
    if not all_heroes:
        return []

    # Second pass: for each map check enemy bans, outcome, and player heroes played.
    times_banned: dict[str, int] = defaultdict(int)
    avail_wins: dict[str, int] = defaultdict(int)
    avail_losses: dict[str, int] = defaultdict(int)
    ban_wins: dict[str, int] = defaultdict(int)
    ban_losses: dict[str, int] = defaultdict(int)
    # pivot_stats only meaningful for heroes with ≥2 maps (likely targets)
    pivot_stats: dict[str, dict] = {h: defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0}) for h in all_heroes}

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            result = get_map_outcome_for_slot(map_entry, our_team_slot)

            enemy_slot = "team2" if our_team_slot == "team1" else "team1"
            draft = map_entry.get("draft", {})
            enemy_draft = draft.get(enemy_slot, {}) if isinstance(draft, dict) else {}
            enemy_bans = {
                _canonical_draft_hero(v)
                for k, v in enemy_draft.items()
                if "ban" in k and _canonical_draft_hero(v)
            }

            player_heroes: set[str] = set()
            player_found = False
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(our_team_slot, []):
                    if (slot.get("player", "") or "").strip().lower() != target_name:
                        continue
                    player_found = True
                    h = _canonical_draft_hero(slot.get("hero", ""))
                    if h:
                        player_heroes.add(h)

            if not player_found:
                continue

            for hero_h in all_heroes:
                if hero_h in enemy_bans:
                    times_banned[hero_h] += 1
                    if result == "Win":
                        ban_wins[hero_h] += 1
                    elif result == "Loss":
                        ban_losses[hero_h] += 1
                    for h in player_heroes:
                        if h != hero_h:
                            pivot_stats[hero_h][h]["count"] += 1
                            if result == "Win":
                                pivot_stats[hero_h][h]["wins"] += 1
                            elif result == "Loss":
                                pivot_stats[hero_h][h]["losses"] += 1
                elif hero_h in player_heroes:
                    if result == "Win":
                        avail_wins[hero_h] += 1
                    elif result == "Loss":
                        avail_losses[hero_h] += 1

    rows = []
    for hero_h in all_heroes:
        a_w = avail_wins[hero_h]
        a_l = avail_losses[hero_h]
        a_dec = a_w + a_l
        hero_wr: float | None = round((a_w / a_dec) * 100, 1) if a_dec else None

        b_w = ban_wins[hero_h]
        b_l = ban_losses[hero_h]
        b_dec = b_w + b_l
        wr_banned: float | None = round((b_w / b_dec) * 100, 1) if b_dec else None

        delta: float | None = round(wr_banned - hero_wr, 1) if (hero_wr is not None and wr_banned is not None) else None

        pvts = []
        for pvt_h, s in pivot_stats[hero_h].items():
            pvt_dec = s["wins"] + s["losses"]
            pvt_wr: float | None = round((s["wins"] / pvt_dec) * 100, 1) if pvt_dec else None
            pvts.append({"hero": pvt_h, "count": s["count"], "wr": pvt_wr})
        pvts.sort(key=lambda x: x["count"], reverse=True)

        rows.append({
            "hero": hero_h,
            "hero_maps": hero_total_maps.get(hero_h, 0),
            "hero_wr": hero_wr,
            "times_banned": times_banned[hero_h],
            "wr_when_banned": wr_banned,
            "wr_delta": delta,
            "all_pivots": pvts,
            "top_pivot": pvts[0] if pvts else None,
        })

    # Sort: heroes that are actually banned first (by ban count), then by maps played
    rows.sort(key=lambda r: (r["times_banned"], r["hero_maps"]), reverse=True)
    return rows


def build_team_ban_impact(scrims: list[dict]) -> list[dict]:
    """Return ban impact rows for every hero in the team's pool with ≥5 maps played.

    Each row contains:
      hero, hero_maps, hero_wr, times_banned, wr_when_banned, wr_delta, all_pivots, top_pivot
    """
    # First pass: count how many maps each hero was played by this team.
    hero_total_maps: dict[str, int] = defaultdict(int)
    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            seen_heroes: set[str] = set()
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(our_team_slot, []):
                    h = _canonical_draft_hero(slot.get("hero", ""))
                    if h and h not in seen_heroes:
                        hero_total_maps[h] += 1
                        seen_heroes.add(h)

    if not hero_total_maps:
        return []

    all_heroes = sorted(
        (h for h in hero_total_maps if hero_total_maps[h] >= 5),
        key=lambda h: hero_total_maps[h], reverse=True,
    )
    if not all_heroes:
        return []

    times_banned: dict[str, int] = defaultdict(int)
    avail_wins: dict[str, int] = defaultdict(int)
    avail_losses: dict[str, int] = defaultdict(int)
    ban_wins: dict[str, int] = defaultdict(int)
    ban_losses: dict[str, int] = defaultdict(int)
    pivot_stats: dict[str, dict] = {h: defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0}) for h in all_heroes}

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            result = get_map_outcome_for_slot(map_entry, our_team_slot)

            enemy_slot = "team2" if our_team_slot == "team1" else "team1"
            draft = map_entry.get("draft", {})
            enemy_draft = draft.get(enemy_slot, {}) if isinstance(draft, dict) else {}
            enemy_bans = {
                _canonical_draft_hero(v)
                for k, v in enemy_draft.items()
                if "ban" in k and _canonical_draft_hero(v)
            }

            team_heroes: set[str] = set()
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(our_team_slot, []):
                    h = _canonical_draft_hero(slot.get("hero", ""))
                    if h:
                        team_heroes.add(h)

            if not team_heroes:
                continue

            for hero_h in all_heroes:
                if hero_h in enemy_bans:
                    times_banned[hero_h] += 1
                    if result == "Win":
                        ban_wins[hero_h] += 1
                    elif result == "Loss":
                        ban_losses[hero_h] += 1
                    for h in team_heroes:
                        if h != hero_h:
                            pivot_stats[hero_h][h]["count"] += 1
                            if result == "Win":
                                pivot_stats[hero_h][h]["wins"] += 1
                            elif result == "Loss":
                                pivot_stats[hero_h][h]["losses"] += 1
                elif hero_h in team_heroes:
                    if result == "Win":
                        avail_wins[hero_h] += 1
                    elif result == "Loss":
                        avail_losses[hero_h] += 1

    rows = []
    for hero_h in all_heroes:
        a_w = avail_wins[hero_h]
        a_l = avail_losses[hero_h]
        a_dec = a_w + a_l
        hero_wr: float | None = round((a_w / a_dec) * 100, 1) if a_dec else None

        b_w = ban_wins[hero_h]
        b_l = ban_losses[hero_h]
        b_dec = b_w + b_l
        wr_banned: float | None = round((b_w / b_dec) * 100, 1) if b_dec else None

        delta: float | None = round(wr_banned - hero_wr, 1) if (hero_wr is not None and wr_banned is not None) else None

        pvts = []
        for pvt_h, s in pivot_stats[hero_h].items():
            pvt_dec = s["wins"] + s["losses"]
            pvt_wr: float | None = round((s["wins"] / pvt_dec) * 100, 1) if pvt_dec else None
            pvts.append({"hero": pvt_h, "count": s["count"], "wr": pvt_wr})
        pvts.sort(key=lambda x: x["count"], reverse=True)

        rows.append({
            "hero": hero_h,
            "hero_maps": hero_total_maps.get(hero_h, 0),
            "hero_wr": hero_wr,
            "times_banned": times_banned[hero_h],
            "wr_when_banned": wr_banned,
            "wr_delta": delta,
            "all_pivots": pvts,
            "top_pivot": pvts[0] if pvts else None,
        })

    rows.sort(key=lambda r: (r["times_banned"], r["hero_maps"]), reverse=True)
    return rows


def build_team_tournament_scrims(team_row: sqlite3.Row | dict) -> list[dict]:
    team_id = int(team_row["id"])
    team_name = (team_row["name"] or "").strip().lower()
    tournament_scrims: list[dict] = []

    for tournament_record in TOURNAMENT_MATCHES:
        for tournament_team in tournament_record.get("tournament_teams", []):
            if not isinstance(tournament_team, dict):
                continue

            source_team_id = tournament_team.get("source_team_id")
            tournament_team_name = (tournament_team.get("name") or "").strip().lower()
            matches_team = (
                (isinstance(source_team_id, int) and source_team_id == team_id)
                or (not source_team_id and tournament_team_name and tournament_team_name == team_name)
            )
            if not matches_team:
                continue

            tournament_scrims.extend(build_tournament_team_scrims(tournament_record, tournament_team))

    return tournament_scrims


def build_team_tournament_rows(team_row: sqlite3.Row | dict) -> list[dict]:
    team_id = int(team_row["id"])
    team_name = (team_row["name"] or "").strip().lower()
    rows: list[dict] = []

    for tournament_record in TOURNAMENT_MATCHES:
        selected_tournament_team: dict | None = None
        for tournament_team in tournament_record.get("tournament_teams", []):
            if not isinstance(tournament_team, dict):
                continue

            source_team_id = tournament_team.get("source_team_id")
            tournament_team_name = (tournament_team.get("name") or "").strip().lower()
            matches_team = (
                (isinstance(source_team_id, int) and source_team_id == team_id)
                or (not source_team_id and tournament_team_name and tournament_team_name == team_name)
            )
            if matches_team:
                selected_tournament_team = tournament_team
                break

        if selected_tournament_team is None:
            continue

        team_scrims = build_tournament_team_scrims(tournament_record, selected_tournament_team)
        analytics = build_scrim_analytics(team_scrims)
        rows.append(
            {
                "tournament_id": tournament_record.get("id"),
                "tournament_name": tournament_record.get("tournament_name") or "Tournament",
                "scrim_date": tournament_record.get("scrim_date", ""),
                "season": tournament_record.get("season", ""),
                "maps": analytics["summary"].get("total_maps", 0),
                "wins": analytics["summary"].get("total_wins", 0),
                "losses": analytics["summary"].get("total_losses", 0),
                "win_rate": analytics["summary"].get("overall_win_rate", 0),
                "tournament_team_id": selected_tournament_team.get("id"),
            }
        )

    rows.sort(key=lambda row: ((row.get("scrim_date") or ""), int(row.get("tournament_id") or 0)), reverse=True)
    return rows


def get_scrim_or_404(scrim_id: int) -> dict:
    for scrim in SCRIMS:
        if scrim["id"] == scrim_id:
            return scrim
    abort(404)


def get_tournament_or_404(tournament_id: int) -> dict:
    for match in TOURNAMENT_MATCHES:
        if match["id"] == tournament_id:
            return match
    abort(404)


def get_tournament_match_or_404(tournament_record: dict, match_id: int) -> dict:
    for tournament_match in tournament_record.get("matches", []):
        if isinstance(tournament_match, dict) and tournament_match.get("id") == match_id:
            return tournament_match
    abort(404)


def get_map_or_404(scrim: dict, map_id: int) -> dict:
    for map_entry in scrim["maps"]:
        if map_entry["id"] == map_id:
            return map_entry
    abort(404)


def parse_comma_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def is_ringer_player_name(raw_name: str | None) -> bool:
    name = str(raw_name or "").strip()
    if not name:
        return False

    lowered = name.lower()
    if re.match(r"^(?:r|ringer|sub|substitute|stand[\s-]?in|merc(?:enary)?)\s*[:\-]", lowered):
        return True
    if re.search(r"[\[(](?:r|ringer|sub|substitute|stand[\s-]?in|merc(?:enary)?)[\])]\s*$", lowered):
        return True

    compact = _compact_text(name)
    return compact in _RINGER_NAME_MARKER_KEYS


def normalize_player_name(raw_name: str | None) -> str:
    name = str(raw_name or "").strip()
    if not name:
        return ""

    name = re.sub(r"\s+", " ", name)
    name = name.strip("`\"'")
    name = re.sub(r"^(?:r|ringer|sub|substitute|stand[\s-]?in|merc(?:enary)?)\s*[:\-]\s*", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*[\[(](?:r|ringer|sub|substitute|stand[\s-]?in|merc(?:enary)?)[\])]\s*$", "", name, flags=re.IGNORECASE)
    name = name.strip()

    if not name:
        return ""

    alias = PLAYER_NAME_ALIASES.get(_compact_text(name))
    return alias or name


def parse_name_list(raw: str) -> list[str]:
    parts = re.split(r"[\r\n,]+", raw or "")
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        original = part.strip()
        if not original or is_ringer_player_name(original):
            continue
        name = normalize_player_name(original)
        if not name:
            continue
        key = _compact_text(name) or name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    return cleaned


def upsert_team_and_players(
    team_name: str,
    player_names: list[str],
    player_main_heroes: dict[str, str] | None = None,
) -> int | None:
    normalized_team_name = str(team_name or "").strip()
    if not normalized_team_name:
        return None

    db = get_db()
    team_row = db.execute(
        "SELECT id FROM teams WHERE lower(name) = lower(?)",
        (normalized_team_name,),
    ).fetchone()

    created_or_updated = False
    if team_row is None:
        db.execute(
            "INSERT INTO teams (name, notes, logo_path, is_personal) VALUES (?, '', '', 0)",
            (normalized_team_name,),
        )
        team_row = db.execute(
            "SELECT id FROM teams WHERE lower(name) = lower(?)",
            (normalized_team_name,),
        ).fetchone()
        created_or_updated = True

    if team_row is None:
        return None

    team_id = int(team_row["id"])
    deduped_players = parse_name_list("\n".join(player_names or []))
    normalized_player_mains = {
        str(player_name).strip().lower(): normalize_hero_slot_value(hero_name)
        for player_name, hero_name in (player_main_heroes or {}).items()
        if str(player_name).strip() and normalize_hero_slot_value(hero_name)
    }
    for player_name in deduped_players:
        player_main_hero = normalized_player_mains.get(player_name.lower(), "")
        try:
            db.execute(
                """
                INSERT INTO players (team_id, name, role, is_sub, main_hero, notes)
                VALUES (?, ?, '', 0, '', '')
                """,
                (team_id, player_name),
            )
            created_or_updated = True
        except sqlite3.IntegrityError:
            pass

        if player_main_hero:
            updated_row = db.execute(
                """
                UPDATE players
                SET main_hero = ?
                WHERE team_id = ?
                  AND lower(name) = lower(?)
                  AND trim(coalesce(main_hero, '')) = ''
                """,
                (player_main_hero, team_id, player_name),
            )
            if updated_row.rowcount:
                created_or_updated = True

    if created_or_updated:
        db.commit()

    return team_id


def _resolve_import_enemy_team_id(enemy_name: str, enemy_lookup: dict[str, int]) -> int | None:
    for enemy_key in _team_name_match_keys(enemy_name):
        if enemy_key in enemy_lookup:
            return enemy_lookup[enemy_key]
    return None


def _collect_scrim_roster_data(scrim: dict) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    players_by_side: dict[str, list[str]] = {
        "team1": list(scrim.get("team1_players", [])),
        "team2": list(scrim.get("team2_players", [])),
    }
    seen_names: dict[str, set[str]] = {
        side: {name.lower() for name in players_by_side.get(side, []) if name}
        for side in TEAM_SLOTS
    }
    hero_counts: dict[str, dict[str, Counter[str]]] = {
        "team1": defaultdict(Counter),
        "team2": defaultdict(Counter),
    }

    for map_entry in scrim.get("maps", []):
        if not isinstance(map_entry, dict):
            continue
        for section in map_entry.get("comp", []):
            if not isinstance(section, dict):
                continue
            for side in TEAM_SLOTS:
                slots = section.get(side, [])
                if not isinstance(slots, list):
                    continue
                for slot in slots:
                    if not isinstance(slot, dict):
                        continue
                    raw_player_name = str(slot.get("player", "")).strip()
                    if is_ringer_player_name(raw_player_name):
                        continue
                    player_name = normalize_player_name(raw_player_name)
                    hero_name = canonicalize_hero_name(slot.get("hero", ""))
                    if player_name:
                        player_key = _compact_text(player_name) or player_name.lower()
                        if player_key not in seen_names[side]:
                            players_by_side[side].append(player_name)
                            seen_names[side].add(player_key)
                    if player_name and hero_name:
                        hero_counts[side][player_name][hero_name] += 1

    player_main_heroes: dict[str, dict[str, str]] = {"team1": {}, "team2": {}}
    for side in TEAM_SLOTS:
        players_by_side[side] = parse_name_list("\n".join(players_by_side[side]))
        for player_name, counts in hero_counts[side].items():
            if not counts:
                continue
            best_hero = max(counts.items(), key=lambda item: (item[1], item[0]))[0]
            if best_hero:
                player_main_heroes[side][player_name] = best_hero

    return players_by_side, player_main_heroes


def _prepare_imported_scrim_context(
    scrim: dict,
    selected_team_id: int,
    selected_team_name: str,
    enemy_lookup: dict[str, int],
) -> None:
    first_map = next((map_entry for map_entry in scrim.get("maps", []) if isinstance(map_entry, dict)), None)
    selected_name = str(selected_team_name or "").strip()
    our_slot = normalize_match_team_slot((first_map or {}).get("our_team_slot", scrim.get("team_slot", "team1")))
    team1_name = str((first_map or {}).get("team1_name") or scrim.get("team1_name") or "").strip()
    team2_name = str((first_map or {}).get("team2_name") or scrim.get("team2_name") or "").strip()

    if our_slot == "team1":
        enemy_name = team2_name or str(scrim.get("enemy_team") or scrim.get("opponent") or "").strip()
        enemy_id = _resolve_import_enemy_team_id(enemy_name, enemy_lookup)
        canonical_enemy_name = get_team_name_by_id(enemy_id) if enemy_id else enemy_name
        scrim.update(
            {
                "team_slot": "team1",
                "team_id": selected_team_id,
                "team_name": selected_name,
                "team1_id": selected_team_id,
                "team1_name": selected_name or team1_name,
                "team2_id": enemy_id,
                "team2_name": canonical_enemy_name,
                "enemy_team_id": enemy_id,
                "enemy_team": canonical_enemy_name,
                "opponent": canonical_enemy_name,
            }
        )
        return

    enemy_name = team1_name or str(scrim.get("enemy_team") or scrim.get("opponent") or "").strip()
    enemy_id = _resolve_import_enemy_team_id(enemy_name, enemy_lookup)
    canonical_enemy_name = get_team_name_by_id(enemy_id) if enemy_id else enemy_name
    scrim.update(
        {
            "team_slot": "team2",
            "team_id": selected_team_id,
            "team_name": selected_name,
            "team1_id": enemy_id,
            "team1_name": canonical_enemy_name,
            "team2_id": selected_team_id,
            "team2_name": selected_name or team2_name,
            "enemy_team_id": enemy_id,
            "enemy_team": canonical_enemy_name,
            "opponent": canonical_enemy_name,
        }
    )


def _sync_scrim_rosters_with_database(scrim: dict) -> None:
    players_by_side, player_main_heroes = _collect_scrim_roster_data(scrim)
    scrim["team1_players"] = players_by_side["team1"]
    scrim["team2_players"] = players_by_side["team2"]

    team1_name = str(scrim.get("team1_name", "")).strip()
    team2_name = str(scrim.get("team2_name", "")).strip()
    team1_id = upsert_team_and_players(team1_name, players_by_side["team1"], player_main_heroes["team1"]) if team1_name else None
    team2_id = upsert_team_and_players(team2_name, players_by_side["team2"], player_main_heroes["team2"]) if team2_name else None

    if team1_id:
        scrim["team1_id"] = team1_id
    if team2_id:
        scrim["team2_id"] = team2_id

    our_slot = normalize_match_team_slot(scrim.get("team_slot", "team1"))
    enemy_slot = "team2" if our_slot == "team1" else "team1"
    scrim["team_id"] = scrim.get(f"{our_slot}_id")
    scrim["team_name"] = str(scrim.get(f"{our_slot}_name", "")).strip()
    scrim["enemy_team_id"] = scrim.get(f"{enemy_slot}_id")
    scrim["enemy_team"] = str(scrim.get(f"{enemy_slot}_name", "")).strip()
    scrim["opponent"] = scrim["enemy_team"]


def _map_name_signature(scrim: dict) -> tuple[str, ...]:
    return tuple(
        _compact_text(map_entry.get("map_name", ""))
        for map_entry in scrim.get("maps", [])
        if isinstance(map_entry, dict) and str(map_entry.get("map_name", "")).strip()
    )


def _find_duplicate_scrim_for_import(imported_scrim: dict, candidates: list[dict] | None = None) -> dict | None:
    imported_team_id = imported_scrim.get("team_id")
    imported_enemy_id = imported_scrim.get("enemy_team_id")
    imported_maps = Counter(_map_name_signature(imported_scrim))
    imported_date = str(imported_scrim.get("scrim_date", "")).strip()
    best_match: dict | None = None
    best_score = -1

    for existing_scrim in (candidates if candidates is not None else SCRIMS):
        if not isinstance(existing_scrim, dict):
            continue
        if str(existing_scrim.get("scrim_date", "")).strip() != imported_date:
            continue

        team_matches = False
        if imported_team_id and existing_scrim.get("team_id") == imported_team_id:
            team_matches = True
        elif _team_names_match(existing_scrim.get("team_name"), imported_scrim.get("team_name")):
            team_matches = True
        if not team_matches:
            continue

        enemy_matches = False
        if imported_enemy_id and existing_scrim.get("enemy_team_id") == imported_enemy_id:
            enemy_matches = True
        elif _team_names_match(existing_scrim.get("enemy_team"), imported_scrim.get("enemy_team")):
            enemy_matches = True
        if not enemy_matches:
            continue

        score = 0
        if imported_team_id and existing_scrim.get("team_id") == imported_team_id:
            score += 20
        if imported_enemy_id and existing_scrim.get("enemy_team_id") == imported_enemy_id:
            score += 20

        existing_maps = Counter(_map_name_signature(existing_scrim))
        overlap = sum((existing_maps & imported_maps).values())
        score += overlap * 5
        if tuple(existing_maps.elements()) == tuple(imported_maps.elements()) and imported_maps:
            score += 15

        if score > best_score:
            best_score = score
            best_match = existing_scrim

    # One-map scrims can still be true duplicates if date/team/opponent/map align,
    # and their score naturally tops out lower than multi-map series.
    return best_match if best_score >= 20 else None


def _merge_imported_map(existing_map: dict, imported_map: dict) -> dict:
    merged_map = copy.deepcopy(imported_map)
    merged_map["id"] = existing_map.get("id")
    if not merged_map.get("notes"):
        merged_map["notes"] = existing_map.get("notes", "")
    if not merged_map.get("vod_url"):
        merged_map["vod_url"] = existing_map.get("vod_url", "")
    if not merged_map.get("events"):
        merged_map["events"] = copy.deepcopy(existing_map.get("events", []))
    return merged_map


def _merge_imported_scrim(existing_scrim: dict, imported_scrim: dict) -> None:
    existing_maps = list(existing_scrim.get("maps", []))
    indexed_existing_maps: dict[str, list[int]] = defaultdict(list)
    for idx, map_entry in enumerate(existing_maps):
        if not isinstance(map_entry, dict):
            continue
        indexed_existing_maps[_compact_text(map_entry.get("map_name", ""))].append(idx)

    merged_scrim = copy.deepcopy(imported_scrim)
    merged_scrim["id"] = existing_scrim.get("id")
    if not merged_scrim.get("notes"):
        merged_scrim["notes"] = existing_scrim.get("notes", "")

    merged_maps: list[dict] = []
    used_indexes: set[int] = set()
    for map_index, imported_map in enumerate(imported_scrim.get("maps", [])):
        map_key = _compact_text(imported_map.get("map_name", ""))
        match_index = next((idx for idx in indexed_existing_maps.get(map_key, []) if idx not in used_indexes), None)
        if match_index is None and map_index < len(existing_maps) and map_index not in used_indexes:
            match_index = map_index

        if match_index is None:
            merged_maps.append(copy.deepcopy(imported_map))
            continue

        used_indexes.add(match_index)
        merged_maps.append(_merge_imported_map(existing_maps[match_index], imported_map))

    merged_scrim["maps"] = merged_maps
    existing_scrim.clear()
    existing_scrim.update(merged_scrim)


def _assign_missing_scrim_ids(scrim: dict) -> None:
    global NEXT_MAP_ID, NEXT_EVENT_ID

    for map_entry in scrim.get("maps", []):
        if not isinstance(map_entry, dict):
            continue
        if not isinstance(map_entry.get("id"), int) or int(map_entry.get("id", 0)) <= 0:
            map_entry["id"] = NEXT_MAP_ID
            NEXT_MAP_ID += 1
        for event in map_entry.get("events", []):
            if not isinstance(event, dict):
                continue
            if not isinstance(event.get("id"), int) or int(event.get("id", 0)) <= 0:
                event["id"] = NEXT_EVENT_ID
                NEXT_EVENT_ID += 1


def _recompute_scrim_next_ids_from_state() -> None:
    global NEXT_SCRIM_ID, NEXT_MAP_ID, NEXT_EVENT_ID

    max_scrim_id = 0
    max_map_id = 0
    max_event_id = 0

    for scrim in SCRIMS:
        if not isinstance(scrim, dict):
            continue
        scrim_id = scrim.get("id")
        if isinstance(scrim_id, int):
            max_scrim_id = max(max_scrim_id, scrim_id)

        for map_entry in scrim.get("maps", []):
            if not isinstance(map_entry, dict):
                continue
            map_id = map_entry.get("id")
            if isinstance(map_id, int):
                max_map_id = max(max_map_id, map_id)

            for event in map_entry.get("events", []):
                if not isinstance(event, dict):
                    continue
                event_id = event.get("id")
                if isinstance(event_id, int):
                    max_event_id = max(max_event_id, event_id)

    NEXT_SCRIM_ID = max(1, max_scrim_id + 1)
    NEXT_MAP_ID = max(1, max_map_id + 1)
    NEXT_EVENT_ID = max(1, max_event_id + 1)


def _dedupe_existing_scrims() -> tuple[int, int]:
    if not SCRIMS:
        return 0, 0

    ordered_scrims = sorted(
        [scrim for scrim in SCRIMS if isinstance(scrim, dict)],
        key=lambda scrim: int(scrim.get("id") or 0),
    )
    survivors: list[dict] = []
    duplicates_removed = 0
    merged_updates = 0

    for scrim in ordered_scrims:
        normalize_scrim_record(scrim)
        existing = _find_duplicate_scrim_for_import(scrim, candidates=survivors)
        if existing is None:
            survivors.append(scrim)
            continue

        _merge_imported_scrim(existing, scrim)
        _assign_missing_scrim_ids(existing)
        duplicates_removed += 1
        merged_updates += 1

    if duplicates_removed:
        SCRIMS[:] = survivors
        _recompute_scrim_next_ids_from_state()

    return duplicates_removed, merged_updates


def find_tournament_team_by_id(tournament_teams: list[dict], tournament_team_id: int | None) -> dict | None:
    if tournament_team_id is None:
        return None
    for team in tournament_teams:
        if isinstance(team, dict) and team.get("id") == tournament_team_id:
            return team
    return None


def find_tournament_team_by_name(tournament_teams: list[dict], team_name: str) -> dict | None:
    candidate = str(team_name or "").strip().lower()
    if not candidate:
        return None
    for team in tournament_teams:
        if isinstance(team, dict) and str(team.get("name", "")).strip().lower() == candidate:
            return team
    return None


def get_tournament_team_by_id(tournament_match: dict, tournament_team_id: int | None) -> dict | None:
    return find_tournament_team_by_id(tournament_match.get("tournament_teams", []), tournament_team_id)


def next_tournament_team_id(tournament_match: dict) -> int:
    max_id = 0
    for team in tournament_match.get("tournament_teams", []):
        if isinstance(team, dict) and isinstance(team.get("id"), int):
            max_id = max(max_id, team["id"])
    return max_id + 1


def next_tournament_match_id(tournament_record: dict) -> int:
    max_id = 0
    for tournament_match in tournament_record.get("matches", []):
        if isinstance(tournament_match, dict) and isinstance(tournament_match.get("id"), int):
            max_id = max(max_id, tournament_match["id"])
    return max_id + 1


def get_result_for_slot(map_entry: dict, slot: str) -> str:
    result = str(map_entry.get("result", "")).strip()
    if result not in {"Win", "Loss"}:
        return result
    original_slot = map_entry.get("our_team_slot", "team1")
    if original_slot not in TEAM_SLOTS:
        original_slot = "team1"
    if slot == original_slot:
        return result
    return "Loss" if result == "Win" else "Win"


def get_map_outcome_for_slot(map_entry: dict, slot: str) -> str:
    left_score, right_score = split_score_pair(map_entry.get("score", ""))
    if left_score.isdigit() and right_score.isdigit():
        left_value = int(left_score)
        right_value = int(right_score)
        if left_value != right_value:
            winner_slot = "team1" if left_value > right_value else "team2"
            return "Win" if winner_slot == slot else "Loss"
    return get_result_for_slot(map_entry, slot)


def infer_result_from_score_text(score_text: str, *, slot: str = "team1") -> str:
    left_score, right_score = split_score_pair(score_text)
    if not (left_score.isdigit() and right_score.isdigit()):
        return ""
    left_value = int(left_score)
    right_value = int(right_score)
    if left_value == right_value:
        return ""
    winner_slot = "team1" if left_value > right_value else "team2"
    return "Win" if winner_slot == slot else "Loss"


def get_tournament_team_slot_for_map(map_entry: dict, tournament_team_id: int | None) -> str | None:
    if tournament_team_id is None:
        return None
    if map_entry.get("team1_tournament_team_id") == tournament_team_id:
        return "team1"
    if map_entry.get("team2_tournament_team_id") == tournament_team_id:
        return "team2"
    return None


def build_tournament_team_scrims(tournament_record: dict, tournament_team: dict) -> list[dict]:
    tournament_team_id = tournament_team.get("id")
    team_scrims: list[dict] = []
    for tournament_match in tournament_record.get("matches", []):
        if tournament_match.get("team1_tournament_team_id") == tournament_team_id:
            opponent_name = tournament_match.get("team2_name") or "Opponent"
        elif tournament_match.get("team2_tournament_team_id") == tournament_team_id:
            opponent_name = tournament_match.get("team1_name") or "Opponent"
        else:
            continue

        remapped_maps: list[dict] = []
        for original_map in tournament_match.get("maps", []):
            if not isinstance(original_map, dict):
                continue
            team_slot = get_tournament_team_slot_for_map(original_map, tournament_team_id)
            if team_slot is None:
                continue
            map_entry = copy.deepcopy(original_map)
            map_entry["our_team_slot"] = team_slot
            map_entry["result"] = get_result_for_slot(original_map, team_slot)
            inferred_result = infer_result_from_score_text(map_entry.get("score", ""), slot=team_slot)
            if inferred_result:
                map_entry["result"] = inferred_result
            remapped_maps.append(map_entry)

        team_scrims.append(
            {
                "id": tournament_match.get("id"),
                "opponent": opponent_name,
                "enemy_team": opponent_name,
                "scrim_date": tournament_match.get("scrim_date") or tournament_record.get("scrim_date", ""),
                "season": tournament_record.get("season", ""),
                "team_id": tournament_record.get("team_id"),
                "team_name": tournament_team.get("name", ""),
                "notes": tournament_match.get("notes", ""),
                "maps": remapped_maps,
            }
        )
    return team_scrims


def build_tournament_match_scrims(tournament_record: dict, perspective: str = "team1") -> list[dict]:
    perspective = perspective if perspective in TEAM_SLOTS else "team1"
    opponent_slot = "team2" if perspective == "team1" else "team1"
    perspective_id_key = f"{perspective}_tournament_team_id"
    opponent_id_key = f"{opponent_slot}_tournament_team_id"
    perspective_name_key = f"{perspective}_name"
    opponent_name_key = f"{opponent_slot}_name"

    tournament_scrims: list[dict] = []
    for tournament_match in tournament_record.get("matches", []):
        perspective_team_id = tournament_match.get(perspective_id_key)
        if perspective_team_id is None:
            continue

        remapped_maps: list[dict] = []
        for original_map in tournament_match.get("maps", []):
            if not isinstance(original_map, dict):
                continue
            team_slot = get_tournament_team_slot_for_map(original_map, perspective_team_id)
            if team_slot is None:
                continue
            map_entry = copy.deepcopy(original_map)
            map_entry["our_team_slot"] = team_slot
            map_entry["result"] = get_result_for_slot(original_map, team_slot)
            inferred_result = infer_result_from_score_text(map_entry.get("score", ""), slot=team_slot)
            if inferred_result:
                map_entry["result"] = inferred_result
            remapped_maps.append(map_entry)

        tournament_scrims.append(
            {
                "id": tournament_match.get("id"),
                "opponent": tournament_match.get(opponent_name_key) or "Opponent",
                "enemy_team": tournament_match.get(opponent_name_key) or "Opponent",
                "scrim_date": tournament_match.get("scrim_date") or tournament_record.get("scrim_date", ""),
                "season": tournament_record.get("season", ""),
                "team_id": tournament_record.get("team_id"),
                "team_name": tournament_match.get(perspective_name_key) or f"Match {perspective.title()}",
                "notes": tournament_match.get("notes", ""),
                "maps": remapped_maps,
                "team1_tournament_team_id": tournament_match.get(perspective_id_key),
                "team2_tournament_team_id": tournament_match.get(opponent_id_key),
            }
        )

    return tournament_scrims


def build_tournament_team_pick_rows(tournament_record: dict, tournament_team: dict) -> list[dict]:
    pick_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})
    tournament_team_id = tournament_team.get("id")

    for tournament_match in tournament_record.get("matches", []):
        if tournament_match.get("team1_tournament_team_id") != tournament_team_id and tournament_match.get("team2_tournament_team_id") != tournament_team_id:
            continue

        for map_entry in tournament_match.get("maps", []):
            if map_entry.get("picked_by_tournament_team_id") != tournament_team_id:
                continue
            team_slot = get_tournament_team_slot_for_map(map_entry, tournament_team_id)
            if team_slot is None:
                continue
            map_name = str(map_entry.get("map_name", "")).strip()
            if not map_name:
                continue
            pick_stats[map_name]["maps"] += 1
            result = get_map_outcome_for_slot(map_entry, team_slot)
            if result == "Win":
                pick_stats[map_name]["wins"] += 1
            elif result == "Loss":
                pick_stats[map_name]["losses"] += 1

    pick_rows = []
    for map_name, stats in pick_stats.items():
        maps_played = stats["maps"]
        pick_rows.append(
            {
                "map_name": map_name,
                "mode": MAP_MODES.get(map_name, "Other"),
                "maps": maps_played,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round((stats["wins"] / maps_played) * 100, 1) if maps_played else 0,
                "image": MAP_IMAGES.get(map_name, ""),
            }
        )
    pick_rows.sort(key=lambda row: (row["maps"], row["win_rate"]), reverse=True)
    return pick_rows


def build_tournament_match_summaries(tournament_record: dict) -> list[dict]:
    match_summaries: list[dict] = []
    for tournament_match in tournament_record.get("matches", []):
        maps_played = len(tournament_match.get("maps", []))
        completed_maps = sum(1 for map_entry in tournament_match.get("maps", []) if map_entry.get("result"))
        picked_maps = sum(1 for map_entry in tournament_match.get("maps", []) if map_entry.get("picked_by_tournament_team_id"))
        match_summaries.append(
            {
                "id": tournament_match.get("id"),
                "team1_name": tournament_match.get("team1_name") or "Team 1",
                "team2_name": tournament_match.get("team2_name") or "Team 2",
                "scrim_date": tournament_match.get("scrim_date") or tournament_record.get("scrim_date", ""),
                "notes": tournament_match.get("notes", ""),
                "maps": maps_played,
                "completed_maps": completed_maps,
                "picked_maps": picked_maps,
            }
        )
    return match_summaries


def build_tournament_overview_analytics(tournament_record: dict) -> dict:
    ban_counts = defaultdict(int)
    protect_counts = defaultdict(int)
    map_stats = defaultdict(
        lambda: {
            "count": 0,
            "completed": 0,
            "wins": 0,
            "losses": 0,
            "mirrored_completed": 0,
            "mirrored_wins": 0,
            "unmirrored_completed": 0,
            "unmirrored_wins": 0,
        }
    )

    total_maps = 0
    total_ban_events = 0
    total_protect_events = 0

    for tournament_match in tournament_record.get("matches", []):
        for map_entry in tournament_match.get("maps", []):
            if not isinstance(map_entry, dict):
                continue

            map_name = str(map_entry.get("map_name", "")).strip()
            if map_name:
                map_stats[map_name]["count"] += 1
                total_maps += 1

            result_value = str(map_entry.get("result", "")).strip()
            if map_name and result_value in {"Win", "Loss"}:
                mirrored = is_map_draft_mirrored(map_entry)
                unmirrored = is_map_draft_unmirrored(map_entry)
                map_stats[map_name]["completed"] += 1
                if result_value == "Win":
                    map_stats[map_name]["wins"] += 1
                else:
                    map_stats[map_name]["losses"] += 1

                if mirrored:
                    map_stats[map_name]["mirrored_completed"] += 1
                    if result_value == "Win":
                        map_stats[map_name]["mirrored_wins"] += 1
                if unmirrored:
                    map_stats[map_name]["unmirrored_completed"] += 1
                    if result_value == "Win":
                        map_stats[map_name]["unmirrored_wins"] += 1

            draft = map_entry.get("draft", {})
            for team_key in ("team1", "team2"):
                team_draft = draft.get(team_key, {}) if isinstance(draft, dict) else {}
                for slot_key in ("ban1", "ban2", "ban3", "ban4"):
                    hero_name = canonicalize_hero_name(team_draft.get(slot_key, ""))
                    if hero_name:
                        ban_counts[hero_name] += 1
                        total_ban_events += 1
                for slot_key in ("protect1", "protect2"):
                    hero_name = canonicalize_hero_name(team_draft.get(slot_key, ""))
                    if hero_name:
                        protect_counts[hero_name] += 1
                        total_protect_events += 1

    ban_rows = []
    for hero_name, count in ban_counts.items():
        ban_rows.append(
            {
                "hero": hero_name,
                "count": count,
                "share": round((count / total_ban_events) * 100, 1) if total_ban_events else 0,
            }
        )
    ban_rows.sort(key=lambda row: (row["count"], row["hero"]), reverse=True)

    protect_rows = []
    for hero_name, count in protect_counts.items():
        protect_rows.append(
            {
                "hero": hero_name,
                "count": count,
                "share": round((count / total_protect_events) * 100, 1) if total_protect_events else 0,
            }
        )
    protect_rows.sort(key=lambda row: (row["count"], row["hero"]), reverse=True)

    map_rows = []
    for map_name, stats in map_stats.items():
        map_rows.append(
            {
                "map_name": map_name,
                "mode": MAP_MODES.get(map_name, "Other"),
                "count": stats["count"],
                "completed": stats["completed"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round((stats["wins"] / stats["completed"]) * 100, 1) if stats["completed"] else None,
                "mirrored_completed": stats["mirrored_completed"],
                "mirrored_win_rate": round((stats["mirrored_wins"] / stats["mirrored_completed"]) * 100, 1) if stats["mirrored_completed"] else None,
                "unmirrored_completed": stats["unmirrored_completed"],
                "unmirrored_win_rate": round((stats["unmirrored_wins"] / stats["unmirrored_completed"]) * 100, 1) if stats["unmirrored_completed"] else None,
                "play_rate": round((stats["count"] / total_maps) * 100, 1) if total_maps else 0,
                "image": MAP_IMAGES.get(map_name, ""),
            }
        )
    map_rows.sort(key=lambda row: (row["count"], row["completed"], row["map_name"]), reverse=True)

    return {
        "summary": {
            "total_maps": total_maps,
            "unique_maps": len(map_rows),
            "total_ban_events": total_ban_events,
            "unique_bans": len(ban_rows),
            "total_protect_events": total_protect_events,
            "unique_protects": len(protect_rows),
        },
        "ban_rows": ban_rows[:12],
        "protect_rows": protect_rows[:12],
        "map_rows": map_rows[:12],
    }


def canonicalize_hero_name(raw_hero: str) -> str:
    return normalize_hero_slot_value(raw_hero)


def is_map_draft_mirrored(map_entry: dict) -> bool:
    draft = map_entry.get("draft", {})
    if not isinstance(draft, dict):
        return False

    team1_draft = draft.get("team1", {}) if isinstance(draft.get("team1", {}), dict) else {}
    team2_draft = draft.get("team2", {}) if isinstance(draft.get("team2", {}), dict) else {}
    team1_heroes = {
        canonicalize_hero_name(hero)
        for hero in team1_draft.values()
        if canonicalize_hero_name(hero)
    }
    team2_heroes = {
        canonicalize_hero_name(hero)
        for hero in team2_draft.values()
        if canonicalize_hero_name(hero)
    }
    if not team1_heroes or not team2_heroes:
        return False
    return len(team1_heroes & team2_heroes) >= 3


def is_map_draft_unmirrored(map_entry: dict) -> bool:
    """Check if a map has unmirrored draft: 1-2 shared heroes between teams (some overlap, but not fully mirrored)."""
    draft = map_entry.get("draft", {})
    if not isinstance(draft, dict):
        return False

    team1_draft = draft.get("team1", {}) if isinstance(draft.get("team1", {}), dict) else {}
    team2_draft = draft.get("team2", {}) if isinstance(draft.get("team2", {}), dict) else {}
    team1_heroes = {
        canonicalize_hero_name(hero)
        for hero in team1_draft.values()
        if canonicalize_hero_name(hero)
    }
    team2_heroes = {
        canonicalize_hero_name(hero)
        for hero in team2_draft.values()
        if canonicalize_hero_name(hero)
    }
    if not team1_heroes or not team2_heroes:
        return False
    shared_count = len(team1_heroes & team2_heroes)
    # Unmirrored: 1-2 shared heroes (not fully mirrored with 3+, but not completely different with 0)
    return 1 <= shared_count <= 2


def normalize_player_role(raw_role: str) -> str:
    candidate = raw_role.strip().lower()
    for role in PLAYER_ROLES:
        if candidate == role.lower():
            return role
    return ""


def build_comp_slot_player_order(player_pool: list[dict], slot_count: int = 6) -> list[str]:
    cleaned: list[dict] = []
    seen_names: set[str] = set()
    for row in player_pool:
        name = str((row or {}).get("name", "")).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        cleaned.append({
            "name": name,
            "role": normalize_player_role(str((row or {}).get("role", ""))),
        })

    desired_roles = ["Vanguard", "Vanguard", "Duelist", "Duelist", "Strategist", "Strategist"]
    if slot_count > len(desired_roles):
        desired_roles.extend([""] * (slot_count - len(desired_roles)))

    buckets = {
        "Vanguard": [row["name"] for row in cleaned if row["role"] == "Vanguard"],
        "Duelist": [row["name"] for row in cleaned if row["role"] == "Duelist"],
        "Strategist": [row["name"] for row in cleaned if row["role"] == "Strategist"],
        "Flex": [row["name"] for row in cleaned if row["role"] == "Flex"],
        "": [row["name"] for row in cleaned if not row["role"]],
    }

    selected: list[str] = []
    selected_keys: set[str] = set()

    def _take_from(role_key: str) -> str:
        queue = buckets.get(role_key, [])
        while queue:
            candidate = queue.pop(0)
            candidate_key = candidate.lower()
            if candidate_key in selected_keys:
                continue
            selected_keys.add(candidate_key)
            return candidate
        return ""

    def _take_any_remaining() -> str:
        for role_key in ("Vanguard", "Duelist", "Strategist", "Flex", ""):
            candidate = _take_from(role_key)
            if candidate:
                return candidate
        return ""

    for desired_role in desired_roles[:slot_count]:
        candidate = _take_from(desired_role) if desired_role else ""
        if not candidate:
            candidate = _take_any_remaining()
        if candidate:
            selected.append(candidate)

    return selected


def team_has_duplicate_heroes(team_slots: list[dict]) -> bool:
    seen: set[str] = set()
    for slot in team_slots:
        hero = str((slot or {}).get("hero", "")).strip()
        if not hero:
            continue
        hero_key = hero.lower()
        if hero_key in seen:
            return True
        seen.add(hero_key)
    return False


def parse_team_id(raw_team_id: str) -> int | None:
    value = (raw_team_id or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    return None


def get_team_name_by_id(team_id: int | None) -> str:
    if team_id is None:
        return ""
    row = get_db().execute("SELECT name FROM teams WHERE id = ?", (team_id,)).fetchone()
    return row["name"] if row else ""


def get_enemy_team_name_by_id(team_id: int | None, enemy_team_id: int | None) -> str:
    if team_id is None or enemy_team_id is None:
        return ""
    row = get_db().execute(
        "SELECT name FROM enemy_teams WHERE id = ? AND team_id = ?",
        (enemy_team_id, team_id),
    ).fetchone()
    return row["name"] if row else ""


def migrate_enemy_teams_to_team_database(db: sqlite3.Connection) -> int:
    """Move legacy enemy-team records into the main team database tables."""
    enemy_rows = db.execute(
        "SELECT id, name, notes, logo_path FROM enemy_teams ORDER BY id"
    ).fetchall()
    if not enemy_rows:
        return 0

    moved_count = 0
    migrated_enemy_ids: list[int] = []

    for enemy_row in enemy_rows:
        enemy_name = (enemy_row["name"] or "").strip()
        if not enemy_name:
            continue

        target_row = db.execute(
            "SELECT id FROM teams WHERE lower(name) = lower(?)",
            (enemy_name,),
        ).fetchone()
        if target_row is None:
            try:
                db.execute(
                    "INSERT INTO teams (name, notes, logo_path, is_personal) VALUES (?, ?, ?, 0)",
                    (enemy_name, enemy_row["notes"] or "", enemy_row["logo_path"] or ""),
                )
            except sqlite3.IntegrityError:
                pass
            target_row = db.execute(
                "SELECT id FROM teams WHERE lower(name) = lower(?)",
                (enemy_name,),
            ).fetchone()

        if target_row is None:
            continue

        target_team_id = target_row["id"]
        player_rows = db.execute(
            "SELECT name, role, main_hero, notes FROM enemy_players WHERE enemy_team_id = ?",
            (enemy_row["id"],),
        ).fetchall()
        for player_row in player_rows:
            player_name = (player_row["name"] or "").strip()
            if not player_name:
                continue
            try:
                db.execute(
                    """
                    INSERT INTO players (team_id, name, role, main_hero, notes)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        target_team_id,
                        player_name,
                        player_row["role"] or "",
                        player_row["main_hero"] or "",
                        player_row["notes"] or "",
                    ),
                )
            except sqlite3.IntegrityError:
                continue

        migrated_enemy_ids.append(enemy_row["id"])
        moved_count += 1

    if migrated_enemy_ids:
        db.executemany("DELETE FROM enemy_teams WHERE id = ?", [(enemy_id,) for enemy_id in migrated_enemy_ids])
        db.commit()

    return moved_count


def build_match_map_entry_from_form() -> dict:
    global NEXT_MAP_ID

    map_name = request.form.get("map_name", "").strip()
    map_type = normalize_map_type_value(request.form.get("map_type", ""))
    result = request.form.get("result", "").strip()
    if result not in RESULTS:
        result = ""

    our_team_slot = request.form.get("our_team_slot", "team1").strip()
    if our_team_slot not in TEAM_SLOTS:
        our_team_slot = "team1"

    draft = {
        "team1": {
            "ban1": request.form.get("team1_ban1", "").strip(),
            "protect1": request.form.get("team1_protect1", "").strip(),
            "ban2": request.form.get("team1_ban2", "").strip(),
            "ban3": request.form.get("team1_ban3", "").strip(),
            "ban4": request.form.get("team1_ban4", "").strip(),
            "protect2": request.form.get("team1_protect2", "").strip(),
        },
        "team2": {
            "ban1": request.form.get("team2_ban1", "").strip(),
            "ban2": request.form.get("team2_ban2", "").strip(),
            "protect1": request.form.get("team2_protect1", "").strip(),
            "ban3": request.form.get("team2_ban3", "").strip(),
            "protect2": request.form.get("team2_protect2", "").strip(),
            "ban4": request.form.get("team2_ban4", "").strip(),
        },
    }

    first_submap = request.form.get("first_submap", "").strip()
    map_entry = {
        "id": NEXT_MAP_ID,
        "map_name": map_name,
        "map_type": map_type,
        "side": "",
        "our_team_slot": our_team_slot,
        "result": result,
        "score": request.form.get("score", "").strip(),
        "draft": draft,
        "comp": build_default_comp_sections(map_name, first_submap=first_submap),
        "notes": request.form.get("notes", "").strip(),
        "vod_url": "",
        "events": [],
    }
    NEXT_MAP_ID += 1
    return map_entry


def build_match_map_detail_context(match_record: dict, map_entry: dict, *, is_tournament: bool, tournament_record: dict | None = None) -> dict:
    if map_entry.get("our_team_slot") not in TEAM_SLOTS:
        map_entry["our_team_slot"] = "team1"
    if map_entry.get("map_type") not in MAP_TYPES:
        map_entry["map_type"] = DEFAULT_MAP_TYPE

    if "comp" not in map_entry or isinstance(map_entry["comp"], dict):
        old = map_entry.get("comp", {})
        if isinstance(old, dict) and "team1" in old:
            map_entry["comp"] = [{
                "submap": "",
                "side": map_entry.get("side", ""),
                "team1": old["team1"],
                "team2": old["team2"],
            }]
        else:
            map_entry["comp"] = build_default_comp_sections(map_entry["map_name"])

    use_section_sides = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), [])) or map_entry.get("map_name") in ATTACK_DEFENSE_MAPS
    for sec in map_entry.get("comp", []):
        sec.setdefault("submap", "")
        sec.setdefault("score", "")
        side_value = (sec.get("side", "") or "").strip()
        if side_value not in SIDES:
            side_value = ""
        sec["side"] = side_value

    team_players = []
    enemy_players = []
    team1_player_options: list[dict] = []
    team2_player_options: list[dict] = []
    team1_default_players: list[str] = []
    team2_default_players: list[str] = []
    enemy_team_data = None
    team1_label = match_record.get("team_name") or match_record.get("team1_name") or "Team 1"
    team2_label = match_record.get("enemy_team") or match_record.get("opponent") or match_record.get("team2_name") or "Team 2"
    participant_one_id = None
    participant_two_id = None
    participant_one_label = ""
    participant_two_label = ""
    picked_by_label = ""

    db = get_db()

    def _build_default_player_slots(player_options: list[dict]) -> list[str]:
        main_pool = [
            {
                "name": (option.get("name") or "").strip(),
                "role": (option.get("role") or "").strip(),
            }
            for option in player_options
            if (option.get("name") or "").strip() and not bool(option.get("is_sub"))
        ]
        return build_comp_slot_player_order(main_pool, slot_count=6)

    if is_tournament:
        tournament_source = tournament_record if tournament_record is not None else match_record
        team1 = get_tournament_team_by_id(tournament_source, map_entry.get("team1_tournament_team_id"))
        team2 = get_tournament_team_by_id(tournament_source, map_entry.get("team2_tournament_team_id"))
        picker = get_tournament_team_by_id(tournament_source, map_entry.get("picked_by_tournament_team_id"))
        team1_label = (team1 or {}).get("name") or map_entry.get("team1_name") or "Team 1"
        team2_label = (team2 or {}).get("name") or map_entry.get("team2_name") or "Team 2"
        map_entry["team1_name"] = team1_label
        map_entry["team2_name"] = team2_label
        map_entry["picked_by_name"] = (picker or {}).get("name") or str(map_entry.get("picked_by_name", "")).strip()
        picked_by_label = map_entry.get("picked_by_name", "")
        team_players = list((team1 or {}).get("players", []))
        if not team_players and team1_label:
            team_row = db.execute(
                "SELECT id FROM teams WHERE lower(name) = lower(?)",
                (team1_label,),
            ).fetchone()
            if team_row:
                team_players = [
                    (row["name"] or "").strip()
                    for row in db.execute(
                        "SELECT name FROM players WHERE team_id = ? AND COALESCE(is_sub, 0) = 0 ORDER BY name COLLATE NOCASE",
                        (team_row["id"],),
                    ).fetchall()
                    if (row["name"] or "").strip()
                ]
        team2_players = list((team2 or {}).get("players", []))
        if not team2_players and team2_label:
            team_row = db.execute(
                "SELECT id FROM teams WHERE lower(name) = lower(?)",
                (team2_label,),
            ).fetchone()
            if team_row:
                team2_players = [
                    (row["name"] or "").strip()
                    for row in db.execute(
                        "SELECT name FROM players WHERE team_id = ? AND COALESCE(is_sub, 0) = 0 ORDER BY name COLLATE NOCASE",
                        (team_row["id"],),
                    ).fetchall()
                    if (row["name"] or "").strip()
                ]
        enemy_players = [
            {
                "name": player_name,
                "role": "",
                "main_hero": "",
            }
            for player_name in team2_players
        ]
        team1_player_options = [{"name": player_name, "role": "", "is_sub": False} for player_name in team_players]
        team2_player_options = [{"name": player_name, "role": "", "is_sub": False} for player_name in team2_players]
    else:
        participant_one, participant_two = get_scrim_participants(match_record)
        participant_one_label, participant_two_label = get_scrim_participant_labels(match_record)
        participant_one_id = participant_one.get("id")
        participant_two_id = participant_two.get("id")
        if not map_entry.get("team1_id") and participant_one_id:
            map_entry["team1_id"] = participant_one_id
        if not map_entry.get("team2_id") and participant_two_id:
            map_entry["team2_id"] = participant_two_id
        if not (map_entry.get("team1_name") or "").strip():
            map_entry["team1_name"] = participant_one_label
        if not (map_entry.get("team2_name") or "").strip():
            map_entry["team2_name"] = participant_two_label
        team1_label = (map_entry.get("team1_name") or "").strip() or participant_one_label
        team2_label = (map_entry.get("team2_name") or "").strip() or participant_two_label

        # Canonicalize side IDs by team names so each side resolves to the
        # correct roster even when legacy ids drift after migrations.
        team1_row = db.execute(
            "SELECT id FROM teams WHERE lower(name) = lower(?)",
            (team1_label,),
        ).fetchone() if team1_label else None
        team2_row = db.execute(
            "SELECT id FROM teams WHERE lower(name) = lower(?)",
            (team2_label,),
        ).fetchone() if team2_label else None

        if team1_row:
            map_entry["team1_id"] = team1_row["id"]
        if team2_row:
            map_entry["team2_id"] = team2_row["id"]

        # If labels are different, never allow both sides to share the same id.
        if (
            (team1_label or "").strip().lower() != (team2_label or "").strip().lower()
            and map_entry.get("team1_id")
            and map_entry.get("team1_id") == map_entry.get("team2_id")
        ):
            if team2_row:
                map_entry["team2_id"] = team2_row["id"]
            elif team1_row:
                map_entry["team1_id"] = team1_row["id"]

        team_id = match_record.get("team_id")
        player_rows = []

        def _load_team_player_options(team_id_value: int | None) -> list[dict]:
            if not team_id_value:
                return []
            rows = db.execute(
                "SELECT name, role, is_sub FROM players WHERE team_id = ? ORDER BY is_sub ASC, name COLLATE NOCASE",
                (team_id_value,),
            ).fetchall()
            return [
                {
                    "name": (row["name"] or "").strip(),
                    "role": (row["role"] or "").strip(),
                    "is_sub": bool(row["is_sub"]),
                }
                for row in rows
                if (row["name"] or "").strip()
            ]

        def _load_enemy_player_options(enemy_team_id_value: int | None) -> list[dict]:
            if not enemy_team_id_value:
                return []
            rows = db.execute(
                "SELECT name, role, is_sub FROM players WHERE team_id = ? ORDER BY is_sub ASC, name COLLATE NOCASE",
                (enemy_team_id_value,),
            ).fetchall()
            if not rows:
                rows = db.execute(
                    "SELECT name, role, 0 as is_sub FROM enemy_players WHERE enemy_team_id = ? ORDER BY name COLLATE NOCASE",
                    (enemy_team_id_value,),
                ).fetchall()
            return [
                {
                    "name": (row["name"] or "").strip(),
                    "role": (row["role"] or "").strip(),
                    "is_sub": bool(row["is_sub"]),
                }
                for row in rows
                if (row["name"] or "").strip()
            ]

        if team_id:
            player_options = _load_team_player_options(team_id)
            player_rows = player_options
            team_players = [row["name"] for row in player_options]

        enemy_team_id = map_entry.get("team2_id") or match_record.get("team2_id") or match_record.get("enemy_team_id")
        if enemy_team_id:
            enemy_team_rows = db.execute(
                "SELECT id, name, notes FROM teams WHERE id = ?",
                (enemy_team_id,),
            ).fetchone()
            if enemy_team_rows:
                enemy_team_data = dict(enemy_team_rows)
                enemy_player_rows = db.execute(
                    "SELECT name, role, main_hero FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
                    (enemy_team_id,),
                ).fetchall()
                enemy_players = [dict(row) for row in enemy_player_rows]

        our_team_id = map_entry.get("team1_id") or match_record.get("team1_id") or match_record.get("team_id")
        enemy_team_id = map_entry.get("team2_id") or match_record.get("team2_id") or match_record.get("enemy_team_id")
        our_team_name = (map_entry.get("team1_name") or match_record.get("team1_name") or match_record.get("team_name") or "").strip().lower()
        enemy_team_name = (map_entry.get("team2_name") or match_record.get("team2_name") or match_record.get("enemy_team") or match_record.get("opponent") or "").strip().lower()

        our_player_options = [dict(row) for row in player_rows]
        if not our_player_options:
            our_player_options = [{"name": player_name, "role": "", "is_sub": False} for player_name in team_players]

        known_enemy_player_options = _load_enemy_player_options(enemy_team_id)
        if not known_enemy_player_options:
            known_enemy_player_options = [
                {
                    "name": (player.get("name") or "").strip(),
                    "role": (player.get("role") or "").strip(),
                    "is_sub": False,
                }
                for player in enemy_players
                if (player.get("name") or "").strip()
            ]

        side_options_cache: dict[tuple[int | None, str], list[dict]] = {}

        def _load_side_options(side_team_id: int | None, side_team_name: str) -> list[dict]:
            cache_key = (side_team_id, (side_team_name or "").strip().lower())
            if cache_key in side_options_cache:
                return list(side_options_cache[cache_key])

            resolved: list[dict] = []
            normalized_side_name = (side_team_name or "").strip().lower()

            if side_team_id and our_team_id and side_team_id == our_team_id:
                resolved = _load_team_player_options(our_team_id)
            elif side_team_id and enemy_team_id and side_team_id == enemy_team_id:
                resolved = _load_enemy_player_options(enemy_team_id)
            elif normalized_side_name and our_team_name and normalized_side_name == our_team_name:
                resolved = _load_team_player_options(our_team_id)
            elif normalized_side_name and enemy_team_name and normalized_side_name == enemy_team_name:
                resolved = _load_enemy_player_options(enemy_team_id)

            if not resolved and side_team_id:
                resolved = _load_team_player_options(side_team_id)
                if not resolved:
                    resolved = _load_enemy_player_options(side_team_id)

            if not resolved and side_team_name:
                team_row = db.execute(
                    "SELECT id FROM teams WHERE lower(name) = lower(?)",
                    ((side_team_name or "").strip(),),
                ).fetchone()
                if team_row:
                    resolved = _load_team_player_options(team_row["id"])

            side_options_cache[cache_key] = list(resolved)
            return list(resolved)

        def _resolve_side_player_options(side_team_id: int | None, side_team_name: str) -> list[dict]:
            direct_match = _load_side_options(side_team_id, side_team_name)
            if direct_match:
                return direct_match

            if side_team_id and our_team_id and side_team_id == our_team_id:
                return list(our_player_options)
            if side_team_id and enemy_team_id and side_team_id == enemy_team_id:
                return list(known_enemy_player_options)

            side_name = (side_team_name or "").strip().lower()
            if side_name and our_team_name and side_name == our_team_name:
                return list(our_player_options)
            if side_name and enemy_team_name and side_name == enemy_team_name:
                return list(known_enemy_player_options)
            return []

        team1_player_options = _resolve_side_player_options(map_entry.get("team1_id"), map_entry.get("team1_name", ""))
        team2_player_options = _resolve_side_player_options(map_entry.get("team2_id"), map_entry.get("team2_name", ""))

    def _extract_comp_players(slot_key: str) -> list[dict]:
        seen: set[str] = set()
        extracted: list[dict] = []
        for section in map_entry.get("comp", []):
            if not isinstance(section, dict):
                continue
            for slot in section.get(slot_key, []):
                if not isinstance(slot, dict):
                    continue
                player_name = (slot.get("player") or "").strip()
                if not player_name:
                    continue
                normalized = player_name.lower()
                if normalized in seen:
                    continue
                seen.add(normalized)
                extracted.append({"name": player_name, "role": "", "is_sub": False})
        return extracted

    if not team1_player_options:
        team1_player_options = _extract_comp_players("team1")
    if not team2_player_options:
        team2_player_options = _extract_comp_players("team2")

    team1_default_players = _build_default_player_slots(team1_player_options)
    team2_default_players = _build_default_player_slots(team2_player_options)

    map_draft_timeline_row = None
    target_map_name = (map_entry.get("map_name") or "").strip()
    if target_map_name:
        source_scrims: list[dict] = []
        if is_tournament:
            perspective = map_entry.get("our_team_slot", "team1") if map_entry.get("our_team_slot", "team1") in TEAM_SLOTS else "team1"
            if tournament_record is not None:
                source_scrims = build_tournament_match_scrims(tournament_record, perspective=perspective)
        else:
            team_id = match_record.get("team_id")
            team_name = (match_record.get("team_name") or match_record.get("team1_name") or "").strip()
            if team_id and team_name:
                source_scrims = get_scrims_for_team(team_id, team_name)

        filtered_scrims: list[dict] = []
        for scrim in source_scrims:
            matching_maps = [m for m in scrim.get("maps", []) if (m.get("map_name") or "").strip() == target_map_name]
            if not matching_maps:
                continue
            scrim_copy = copy.deepcopy(scrim)
            scrim_copy["maps"] = matching_maps
            filtered_scrims.append(scrim_copy)

        if filtered_scrims:
            map_timeline = build_draft_phase_timeline(filtered_scrims)
            map_draft_timeline_row = next(
                (row for row in map_timeline.get("maps", []) if row.get("map_name") == target_map_name),
                None,
            )

    return {
        "match_record": match_record,
        "map_entry": map_entry,
        "heroes": HEROES,
        "hero_roles": HERO_ROLES,
        "hero_transformations": HERO_TRANSFORMATIONS,
        "map_images": MAP_IMAGES,
        "map_submaps": MAP_SUBMAPS,
        "map_mode": MAP_MODES.get(map_entry.get("map_name", ""), "Other"),
        "maps": MAPS,
        "sides": SIDES,
        "results": RESULTS,
        "event_types": EVENT_TYPES,
        "team_players": team_players,
        "enemy_team": enemy_team_data,
        "enemy_players": enemy_players,
        "team1_player_options": team1_player_options,
        "team2_player_options": team2_player_options,
        "team1_default_players": team1_default_players,
        "team2_default_players": team2_default_players,
        "team1_label": team1_label,
        "team2_label": team2_label,
        "participant_one_id": participant_one_id,
        "participant_two_id": participant_two_id,
        "participant_one_label": participant_one_label,
        "participant_two_label": participant_two_label,
        "picked_by_label": picked_by_label,
        "map_draft_timeline_row": map_draft_timeline_row,
        "split_score_pair": split_score_pair,
    }


def save_team_logo(file: FileStorage | None, team_name: str) -> str:
    if file is None or not file.filename:
        return ""

    raw_name = secure_filename(file.filename)
    ext = Path(raw_name).suffix.lower()
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        return ""

    safe_team = secure_filename(team_name.strip()) or "team"
    filename = f"{safe_team}-{uuid4().hex[:10]}{ext}"
    TEAM_LOGO_DIR.mkdir(parents=True, exist_ok=True)
    destination = TEAM_LOGO_DIR / filename
    file.save(destination)
    # When logos live on a persistent disk outside static/, store the bare
    # filename so the /team-logo/<filename> route can serve it.
    if _LOGOS_ON_DISK:
        return f"__disk__/{filename}"
    return f"uploads/team_logos/{filename}"


def _resolve_logo_file_path(relative_path: str) -> Path | None:
    """Return the absolute Path for a stored logo_path value, or None."""
    if not relative_path:
        return None
    if relative_path.startswith("__disk__/"):
        filename = relative_path[len("__disk__/"):]
        return TEAM_LOGO_DIR / filename
    return Path(app.static_folder) / relative_path


def delete_team_logo_file(relative_path: str) -> None:
    if not relative_path:
        return
    logo_path = _resolve_logo_file_path(relative_path)
    if logo_path is None:
        return
    try:
        if logo_path.exists() and logo_path.is_file():
            logo_path.unlink()
    except OSError:
        # Failing to remove an old logo file should not block team updates.
        pass


@app.route("/team-logo/<path:filename>")
def serve_team_logo(filename: str):
    """Serve team logo files stored on the persistent disk (outside static/)."""
    from flask import send_from_directory
    safe = secure_filename(filename)
    if not safe:
        abort(404)
    return send_from_directory(str(TEAM_LOGO_DIR), safe)


def build_default_comp_sections(map_name: str, first_submap: str = "") -> list[dict]:
    submaps = MAP_SUBMAPS.get(map_name, [])
    if submaps:
        ordered_submaps = list(submaps)
        chosen_submap = (first_submap or "").strip()
        if chosen_submap in ordered_submaps:
            start_index = ordered_submaps.index(chosen_submap)
            ordered_submaps = ordered_submaps[start_index:] + ordered_submaps[:start_index]
        return [
            {
                "submap": sm,
                "side": "",
                "score": "",
                "team1": [{"hero": "", "player": ""} for _ in range(6)],
                "team2": [{"hero": "", "player": ""} for _ in range(6)],
            }
            for sm in ordered_submaps
        ]

    if map_name in ATTACK_DEFENSE_MAPS:
        return [
            {
                "submap": "",
                "side": side,
                "score": "",
                "team1": [{"hero": "", "player": ""} for _ in range(6)],
                "team2": [{"hero": "", "player": ""} for _ in range(6)],
            }
            for side in SIDES
        ]

    return [
        {
            "submap": "",
            "side": "",
            "score": "",
            "team1": [{"hero": "", "player": ""} for _ in range(6)],
            "team2": [{"hero": "", "player": ""} for _ in range(6)],
        }
    ]


def build_scrim_analytics(
    scrims: list[dict],
    *,
    perspective_label: str = "Team",
    opponent_label: str = "Opponent",
    roster_player_names: list[str] | set[str] | None = None,
) -> dict:
    ban_slot_keys = ("ban1", "ban2", "ban3", "ban4")
    ban_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    enemy_ban_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    ban_position_stats = {slot: defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0}) for slot in ban_slot_keys}
    enemy_ban_position_stats = {slot: defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0}) for slot in ban_slot_keys}
    protect_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    hero_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "unmirrored_maps": 0, "unmirrored_wins": 0, "unmirrored_losses": 0})
    map_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})
    map_draft_stats = defaultdict(
        lambda: {
            "ban_totals": 0,
            "protect_totals": 0,
            "ban_heroes": defaultdict(int),
            "protect_heroes": defaultdict(int),
        }
    )
    comp_profile_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    enemy_comp_profile_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    draft_mirror_total = 0
    draft_soft_mirror_count = 0
    draft_hard_mirror_count = 0
    comp_mirror_total = 0
    comp_soft_mirror_count = 0
    comp_hard_mirror_count = 0
    ban_next_pairs = defaultdict(lambda: defaultdict(int))
    ban_to_protect_pairs = defaultdict(lambda: defaultdict(int))
    draft_route_counts = defaultdict(int)
    draft_route_from_totals = defaultdict(int)
    lead_source_counts = {
        "ban": defaultdict(lambda: defaultdict(int)),
        "protect": defaultdict(lambda: defaultdict(int)),
    }
    lead_target_totals = {
        "ban": defaultdict(int),
        "protect": defaultdict(int),
    }
    second_order_ban_targets = defaultdict(
        lambda: {
            "ban3": defaultdict(int),
            "protect2": defaultdict(int),
            "ban4": defaultdict(int),
            "totals": {"source": 0, "ban3": 0, "protect2": 0, "ban4": 0},
        }
    )
    protect1_influence_targets = defaultdict(
        lambda: {
            "ban2": defaultdict(int),
            "ban3": defaultdict(int),
            "protect2": defaultdict(int),
            "ban4": defaultdict(int),
            "totals": {"source": 0, "ban2": 0, "ban3": 0, "protect2": 0, "ban4": 0},
        }
    )
    ban1_protect1_route_targets = defaultdict(
        lambda: {
            "ban2": defaultdict(int),
            "ban3": defaultdict(int),
            "protect2": defaultdict(int),
            "ban4": defaultdict(int),
            "totals": {"source": 0, "ban2": 0, "ban3": 0, "protect2": 0, "ban4": 0},
        }
    )
    hero_open_stats = defaultdict(
        lambda: {
            "open_maps": 0,
            "open_wins": 0,
            "open_losses": 0,
            "played_when_open": 0,
            "played_wins": 0,
            "played_losses": 0,
            "fully_open_maps": 0,
            "our_played_when_fully_open": 0,
            "enemy_played_when_fully_open": 0,
            "teammate_open_counts": defaultdict(int),
            "closed_maps": 0,
            "closed_wins": 0,
            "closed_losses": 0,
        }
    )

    total_maps = 0
    total_wins = 0
    total_losses = 0
    total_filled_bans = 0
    total_enemy_filled_bans = 0
    ban_position_totals = defaultdict(int)
    enemy_ban_position_totals = defaultdict(int)
    total_filled_protects = 0
    roster_player_keys = {
        (player_name or "").strip().lower()
        for player_name in (roster_player_names or [])
        if (player_name or "").strip()
    }

    def classify_comp_profile(heroes: list[str]) -> str:
        role_counts = defaultdict(int)
        for hero_name in heroes:
            role_name = _hero_role(hero_name)
            if role_name:
                role_counts[role_name] += 1

        strategist_count = role_counts.get("Strategist", 0) + role_counts.get("Support", 0)
        dps_count = role_counts.get("Duelist", 0) + role_counts.get("DPS", 0)
        tank_count = role_counts.get("Vanguard", 0) + role_counts.get("Tank", 0)

        if strategist_count >= 3:
            return "triple_support"
        if tank_count >= 3:
            return "triple_tank"
        if strategist_count == 2 and dps_count == 2 and tank_count == 2:
            return "two_two_two"
        return "other"

    def canonical_hero(raw_hero: str) -> str:
        hero_text = (raw_hero or "").strip()
        if not hero_text:
            return ""
        return _resolve_hero_transform_key(hero_text) or hero_text

    def draft_slot_label(slot_key: str) -> str:
        if slot_key.startswith("ban"):
            return f"Ban {slot_key[-1]}"
        if slot_key.startswith("protect"):
            return f"Protect {slot_key[-1]}"
        return slot_key

    hero_pool = {
        canonical_hero(hero_name)
        for hero_name in HEROES
        if canonical_hero(hero_name)
    }

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            total_maps += 1
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            map_outcome = get_map_outcome_for_slot(map_entry, our_team_slot)
            is_win = map_outcome == "Win"
            is_loss = map_outcome == "Loss"
            if is_win:
                total_wins += 1
            elif is_loss:
                total_losses += 1

            map_name = map_entry.get("map_name", "").strip()
            if map_name:
                map_stats[map_name]["maps"] += 1
                if is_win:
                    map_stats[map_name]["wins"] += 1
                elif is_loss:
                    map_stats[map_name]["losses"] += 1

            draft = map_entry.get("draft", {})
            our_draft = draft.get(our_team_slot, {})
            enemy_draft = draft.get(opposite_team_slot(our_team_slot), {})

            our_ban_slots = {
                slot: canonical_hero(our_draft.get(slot, ""))
                for slot in ("ban1", "ban2", "ban3", "ban4")
            }
            enemy_ban_slots = {
                slot: canonical_hero(enemy_draft.get(slot, ""))
                for slot in ("ban1", "ban2", "ban3", "ban4")
            }
            our_protect_slots = {
                slot: canonical_hero(our_draft.get(slot, ""))
                for slot in ("protect1", "protect2")
            }
            our_banned_heroes = {
                hero_name
                for hero_name in our_ban_slots.values()
                if hero_name
            }
            enemy_banned_heroes = {
                hero_name
                for hero_name in enemy_ban_slots.values()
                if hero_name
            }

            # Ban response likelihood: when we ban X in a slot, what the enemy bans
            # in their corresponding next ban slot.
            for slot in ("ban1", "ban2", "ban3", "ban4"):
                source_ban = our_ban_slots.get(slot, "")
                response_ban = enemy_ban_slots.get(slot, "")
                if source_ban and response_ban:
                    ban_next_pairs[source_ban][response_ban] += 1

            # Ban -> Protect flow based on draft phases:
            # Phase 1: ban1 leads into protect1.
            # Phase 3: protect2 happens after ban1-3, while ban4 is final.
            if our_ban_slots.get("ban1") and our_protect_slots.get("protect1"):
                ban_to_protect_pairs[our_ban_slots["ban1"]][our_protect_slots["protect1"]] += 1

                route_key = (our_ban_slots["ban1"], our_protect_slots["protect1"])
                ban1_protect1_route_targets[route_key]["totals"]["source"] += 1
                if our_ban_slots.get("ban2"):
                    ban1_protect1_route_targets[route_key]["ban2"][our_ban_slots["ban2"]] += 1
                    ban1_protect1_route_targets[route_key]["totals"]["ban2"] += 1
                if our_ban_slots.get("ban3"):
                    ban1_protect1_route_targets[route_key]["ban3"][our_ban_slots["ban3"]] += 1
                    ban1_protect1_route_targets[route_key]["totals"]["ban3"] += 1
                if our_protect_slots.get("protect2"):
                    ban1_protect1_route_targets[route_key]["protect2"][our_protect_slots["protect2"]] += 1
                    ban1_protect1_route_targets[route_key]["totals"]["protect2"] += 1
                if our_ban_slots.get("ban4"):
                    ban1_protect1_route_targets[route_key]["ban4"][our_ban_slots["ban4"]] += 1
                    ban1_protect1_route_targets[route_key]["totals"]["ban4"] += 1

            if our_protect_slots.get("protect2"):
                for slot in ("ban1", "ban2", "ban3"):
                    if our_ban_slots.get(slot):
                        ban_to_protect_pairs[our_ban_slots[slot]][our_protect_slots["protect2"]] += 1

            ban1_hero = our_ban_slots.get("ban1", "")
            ban2_hero = our_ban_slots.get("ban2", "")
            if ban1_hero and ban2_hero:
                second_order_ban_targets[(ban1_hero, ban2_hero)]["totals"]["source"] += 1
                ban3_hero = our_ban_slots.get("ban3", "")
                protect2_hero = our_protect_slots.get("protect2", "")
                ban4_hero = our_ban_slots.get("ban4", "")
                if ban3_hero:
                    second_order_ban_targets[(ban1_hero, ban2_hero)]["ban3"][ban3_hero] += 1
                    second_order_ban_targets[(ban1_hero, ban2_hero)]["totals"]["ban3"] += 1
                if protect2_hero:
                    second_order_ban_targets[(ban1_hero, ban2_hero)]["protect2"][protect2_hero] += 1
                    second_order_ban_targets[(ban1_hero, ban2_hero)]["totals"]["protect2"] += 1
                if ban4_hero:
                    second_order_ban_targets[(ban1_hero, ban2_hero)]["ban4"][ban4_hero] += 1
                    second_order_ban_targets[(ban1_hero, ban2_hero)]["totals"]["ban4"] += 1

            protect1_hero = our_protect_slots.get("protect1", "")
            if protect1_hero:
                protect1_influence_targets[protect1_hero]["totals"]["source"] += 1
                if our_ban_slots.get("ban2"):
                    protect1_influence_targets[protect1_hero]["ban2"][our_ban_slots["ban2"]] += 1
                    protect1_influence_targets[protect1_hero]["totals"]["ban2"] += 1
                if our_ban_slots.get("ban3"):
                    protect1_influence_targets[protect1_hero]["ban3"][our_ban_slots["ban3"]] += 1
                    protect1_influence_targets[protect1_hero]["totals"]["ban3"] += 1
                if our_protect_slots.get("protect2"):
                    protect1_influence_targets[protect1_hero]["protect2"][our_protect_slots["protect2"]] += 1
                    protect1_influence_targets[protect1_hero]["totals"]["protect2"] += 1
                if our_ban_slots.get("ban4"):
                    protect1_influence_targets[protect1_hero]["ban4"][our_ban_slots["ban4"]] += 1
                    protect1_influence_targets[protect1_hero]["totals"]["ban4"] += 1

            draft_sequence = [
                (slot, canonical_hero(our_draft.get(slot, "")))
                for slot in ("ban1", "protect1", "ban2", "ban3", "protect2", "ban4")
                if canonical_hero(our_draft.get(slot, ""))
            ]

            for idx in range(len(draft_sequence) - 1):
                from_slot, from_hero = draft_sequence[idx]
                to_slot, to_hero = draft_sequence[idx + 1]
                draft_route_counts[(from_slot, from_hero, to_slot, to_hero)] += 1
                draft_route_from_totals[(from_slot, from_hero)] += 1

            for idx in range(1, len(draft_sequence)):
                target_slot, target_hero = draft_sequence[idx]
                target_type = "ban" if target_slot.startswith("ban") else "protect"
                lead_target_totals[target_type][target_hero] += 1
                for prev_slot, prev_hero in draft_sequence[:idx]:
                    source_key = f"{draft_slot_label(prev_slot)}|{prev_hero}"
                    lead_source_counts[target_type][target_hero][source_key] += 1

            our_draft_heroes = [
                (_resolve_hero_transform_key((hero or "").strip()) or (hero or "").strip())
                for hero in our_draft.values()
                if (hero or "").strip()
            ]
            enemy_draft_heroes = [
                (_resolve_hero_transform_key((hero or "").strip()) or (hero or "").strip())
                for hero in enemy_draft.values()
                if (hero or "").strip()
            ]
            if our_draft_heroes and enemy_draft_heroes:
                shared_draft_heroes = len(set(our_draft_heroes) & set(enemy_draft_heroes))
                draft_mirror_total += 1
                if shared_draft_heroes >= 4:
                    draft_soft_mirror_count += 1

            for slot_key, hero in our_draft.items():
                hero_name = (hero or "").strip()
                if not hero_name:
                    continue

                if "ban" in slot_key:
                    total_filled_bans += 1
                    ban_stats[hero_name]["count"] += 1
                    if map_name:
                        map_draft_stats[map_name]["ban_totals"] += 1
                        map_draft_stats[map_name]["ban_heroes"][hero_name] += 1
                    if slot_key in ban_position_stats:
                        ban_position_totals[slot_key] += 1
                        ban_position_stats[slot_key][hero_name]["count"] += 1
                    if is_win:
                        ban_stats[hero_name]["wins"] += 1
                        if slot_key in ban_position_stats:
                            ban_position_stats[slot_key][hero_name]["wins"] += 1
                    elif is_loss:
                        ban_stats[hero_name]["losses"] += 1
                        if slot_key in ban_position_stats:
                            ban_position_stats[slot_key][hero_name]["losses"] += 1
                elif "protect" in slot_key:
                    total_filled_protects += 1
                    protect_stats[hero_name]["count"] += 1
                    if map_name:
                        map_draft_stats[map_name]["protect_totals"] += 1
                        map_draft_stats[map_name]["protect_heroes"][hero_name] += 1
                    if is_win:
                        protect_stats[hero_name]["wins"] += 1
                    elif is_loss:
                        protect_stats[hero_name]["losses"] += 1

            for slot_key, hero in enemy_draft.items():
                hero_name = (hero or "").strip()
                if not hero_name or "ban" not in slot_key:
                    continue

                total_enemy_filled_bans += 1
                enemy_ban_stats[hero_name]["count"] += 1
                if slot_key in enemy_ban_position_stats:
                    enemy_ban_position_totals[slot_key] += 1
                    enemy_ban_position_stats[slot_key][hero_name]["count"] += 1
                if is_win:
                    enemy_ban_stats[hero_name]["wins"] += 1
                    if slot_key in enemy_ban_position_stats:
                        enemy_ban_position_stats[slot_key][hero_name]["wins"] += 1
                elif is_loss:
                    enemy_ban_stats[hero_name]["losses"] += 1
                    if slot_key in enemy_ban_position_stats:
                        enemy_ban_position_stats[slot_key][hero_name]["losses"] += 1

            hero_instances_in_map: list[str] = []
            comp_profiles_in_map = set()
            enemy_comp_profiles_in_map = set()
            for section in map_entry.get("comp", []):
                hero_instances_in_map.extend(_canonical_section_hero_instances(section, our_team_slot))

                section_heroes = _canonical_section_hero_instances(section, our_team_slot)
                if section_heroes:
                    comp_profiles_in_map.add(classify_comp_profile(section_heroes))

                enemy_section_heroes = _canonical_section_hero_instances(section, opposite_team_slot(our_team_slot))
                if enemy_section_heroes:
                    enemy_comp_profiles_in_map.add(classify_comp_profile(enemy_section_heroes))

                if section_heroes and enemy_section_heroes:
                    shared_comp_heroes = len(set(section_heroes) & set(enemy_section_heroes))
                    comp_mirror_total += 1
                    if shared_comp_heroes >= 4:
                        comp_soft_mirror_count += 1

            canonical_heroes_in_map = {
                canonical_hero(hero_name)
                for hero_name in hero_instances_in_map
                if canonical_hero(hero_name)
            }
            enemy_hero_instances_in_map = _canonical_map_hero_instances(map_entry, opposite_team_slot(our_team_slot))
            enemy_canonical_heroes_in_map = {
                canonical_hero(hero_name)
                for hero_name in enemy_hero_instances_in_map
                if canonical_hero(hero_name)
            }
            our_hero_players_in_map = defaultdict(set)
            for section in map_entry.get("comp", []):
                for slot in section.get(our_team_slot, []):
                    if not isinstance(slot, dict):
                        continue
                    hero_name = canonical_hero(slot.get("hero", ""))
                    player_name = (slot.get("player", "") or "").strip()
                    player_key = player_name.lower()
                    if hero_name and player_name and (
                        not roster_player_keys or player_key in roster_player_keys
                    ):
                        our_hero_players_in_map[hero_name].add(player_name)

            # Only track open/closed stats on maps where the draft was actually logged.
            # Without ban data, every hero in hero_pool looks "open" (empty banned set),
            # which inflates open_maps for all heroes on draft-less maps.
            has_draft_data = (
                any(our_ban_slots.values())
                or any(enemy_ban_slots.values())
                or any(our_protect_slots.values())
            )
            if not has_draft_data:
                # No draft was recorded for this map; skip open/closed tracking.
                pass
            else:
                tracked_heroes = hero_pool | enemy_banned_heroes | canonical_heroes_in_map
                for hero_name in tracked_heroes:
                    is_open = hero_name not in enemy_banned_heroes
                    is_played = hero_name in canonical_heroes_in_map
                    if is_open:
                        hero_open_stats[hero_name]["open_maps"] += 1
                        if is_win:
                            hero_open_stats[hero_name]["open_wins"] += 1
                        elif is_loss:
                            hero_open_stats[hero_name]["open_losses"] += 1

                        if is_played:
                            hero_open_stats[hero_name]["played_when_open"] += 1
                            if is_win:
                                hero_open_stats[hero_name]["played_wins"] += 1
                            elif is_loss:
                                hero_open_stats[hero_name]["played_losses"] += 1
                            for player_name in our_hero_players_in_map.get(hero_name, []):
                                hero_open_stats[hero_name]["teammate_open_counts"][player_name] += 1

                    is_fully_open = hero_name not in enemy_banned_heroes and hero_name not in our_banned_heroes
                    if is_fully_open:
                        hero_open_stats[hero_name]["fully_open_maps"] += 1
                        if hero_name in canonical_heroes_in_map:
                            hero_open_stats[hero_name]["our_played_when_fully_open"] += 1
                        if hero_name in enemy_canonical_heroes_in_map:
                            hero_open_stats[hero_name]["enemy_played_when_fully_open"] += 1

                    # "Banned" means the enemy specifically banned the hero (not us banning it).
                    # Tracked separately from is_fully_open so a hero we ban doesn't pollute
                    # the "WR When Banned" win-rate or inflate open_maps vs closed_maps totals.
                    if not is_open:
                        hero_open_stats[hero_name]["closed_maps"] += 1
                        if is_win:
                            hero_open_stats[hero_name]["closed_wins"] += 1
                        elif is_loss:
                            hero_open_stats[hero_name]["closed_losses"] += 1

            # Determine if draft is unmirrored (1-2 shared heroes)
            is_draft_unmirrored = False
            if our_draft_heroes and enemy_draft_heroes:
                shared_draft_heroes = len(set(our_draft_heroes) & set(enemy_draft_heroes))
                is_draft_unmirrored = 1 <= shared_draft_heroes <= 2

            for hero_name in hero_instances_in_map:
                hero_stats[hero_name]["maps"] += 1
                if is_win:
                    hero_stats[hero_name]["wins"] += 1
                elif is_loss:
                    hero_stats[hero_name]["losses"] += 1
                
                if is_draft_unmirrored:
                    hero_stats[hero_name]["unmirrored_maps"] += 1
                    if is_win:
                        hero_stats[hero_name]["unmirrored_wins"] += 1
                    elif is_loss:
                        hero_stats[hero_name]["unmirrored_losses"] += 1

            for profile_key in comp_profiles_in_map:
                comp_profile_stats[profile_key]["count"] += 1
                if is_win:
                    comp_profile_stats[profile_key]["wins"] += 1
                elif is_loss:
                    comp_profile_stats[profile_key]["losses"] += 1

            for profile_key in enemy_comp_profiles_in_map:
                enemy_comp_profile_stats[profile_key]["count"] += 1
                if is_win:
                    enemy_comp_profile_stats[profile_key]["wins"] += 1
                elif is_loss:
                    enemy_comp_profile_stats[profile_key]["losses"] += 1

    def pct(part: int, whole: int) -> float:
        return round((part / whole) * 100, 1) if whole else 0.0

    ban_rows = []
    for hero, stats in ban_stats.items():
        ban_rows.append(
            {
                "hero": hero,
                "count": stats["count"],
                "ban_rate": pct(stats["count"], total_filled_bans),
            }
        )
    ban_rows.sort(key=lambda r: r["count"], reverse=True)

    enemy_ban_rows = []
    for hero, stats in enemy_ban_stats.items():
        enemy_ban_rows.append(
            {
                "hero": hero,
                "count": stats["count"],
                "ban_rate": pct(stats["count"], total_enemy_filled_bans),
            }
        )
    enemy_ban_rows.sort(key=lambda r: r["count"], reverse=True)

    ban_next_rows = []
    for source_hero, response_counts in ban_next_pairs.items():
        total_sequences = sum(response_counts.values())
        response_rows = sorted(response_counts.items(), key=lambda item: item[1], reverse=True)
        top_response, top_count = response_rows[0] if response_rows else ("", 0)
        ban_next_rows.append(
            {
                "ban_hero": source_hero,
                "total": total_sequences,
                "top_enemy_ban": top_response,
                "top_count": top_count,
                "top_rate": pct(top_count, total_sequences),
                "responses": [
                    {
                        "hero": response_hero,
                        "count": response_count,
                        "rate": pct(response_count, total_sequences),
                    }
                    for response_hero, response_count in response_rows[:3]
                ],
            }
        )
    ban_next_rows.sort(key=lambda row: (row["total"], row["top_rate"]), reverse=True)

    ban_to_protect_rows = []
    for source_hero, protect_counts in ban_to_protect_pairs.items():
        total_links = sum(protect_counts.values())
        protect_rows = sorted(protect_counts.items(), key=lambda item: item[1], reverse=True)
        top_protect, top_count = protect_rows[0] if protect_rows else ("", 0)
        ban_to_protect_rows.append(
            {
                "ban_hero": source_hero,
                "total": total_links,
                "top_protect": top_protect,
                "top_count": top_count,
                "top_rate": pct(top_count, total_links),
                "protects": [
                    {
                        "hero": protect_hero,
                        "count": protect_count,
                        "rate": pct(protect_count, total_links),
                    }
                    for protect_hero, protect_count in protect_rows[:3]
                ],
            }
        )
    ban_to_protect_rows.sort(key=lambda row: (row["total"], row["top_rate"]), reverse=True)

    draft_route_rows = []
    for (from_slot, from_hero, to_slot, to_hero), count in draft_route_counts.items():
        total_from = draft_route_from_totals[(from_slot, from_hero)]
        draft_route_rows.append(
            {
                "from_slot": draft_slot_label(from_slot),
                "from_hero": from_hero,
                "to_slot": draft_slot_label(to_slot),
                "to_hero": to_hero,
                "count": count,
                "rate": pct(count, total_from),
            }
        )
    draft_route_rows.sort(key=lambda row: (row["count"], row["rate"]), reverse=True)

    def build_lead_rows(target_type: str) -> list[dict]:
        rows = []
        for target_hero, source_counts in lead_source_counts[target_type].items():
            total = lead_target_totals[target_type][target_hero]
            sorted_sources = sorted(source_counts.items(), key=lambda item: item[1], reverse=True)
            if not sorted_sources:
                continue
            top_source_key, top_count = sorted_sources[0]
            top_slot, top_hero = top_source_key.split("|", 1)
            rows.append(
                {
                    "target_hero": target_hero,
                    "total": total,
                    "top_source_slot": top_slot,
                    "top_source_hero": top_hero,
                    "top_count": top_count,
                    "top_rate": pct(top_count, total),
                }
            )
        rows.sort(key=lambda row: (row["total"], row["top_rate"]), reverse=True)
        return rows

    lead_to_ban_rows = build_lead_rows("ban")
    lead_to_protect_rows = build_lead_rows("protect")

    total_second_order_sources = sum(
        target_data["totals"].get("source", 0)
        for target_data in second_order_ban_targets.values()
    )

    second_order_ban_rows = []
    for (ban1_hero, ban2_hero), target_data in second_order_ban_targets.items():
        source_total = target_data["totals"].get("source", 0)
        ban3_total = target_data["totals"]["ban3"]
        protect2_total = target_data["totals"]["protect2"]
        ban4_total = target_data["totals"]["ban4"]

        ban3_sorted = sorted(target_data["ban3"].items(), key=lambda item: item[1], reverse=True)
        protect2_sorted = sorted(target_data["protect2"].items(), key=lambda item: item[1], reverse=True)
        ban4_sorted = sorted(target_data["ban4"].items(), key=lambda item: item[1], reverse=True)

        top_ban3, top_ban3_count = ban3_sorted[0] if ban3_sorted else ("", 0)
        top_protect2, top_protect2_count = protect2_sorted[0] if protect2_sorted else ("", 0)
        top_ban4, top_ban4_count = ban4_sorted[0] if ban4_sorted else ("", 0)

        second_order_ban_rows.append(
            {
                "ban1_hero": ban1_hero,
                "ban1_rate": pct(source_total, total_second_order_sources),
                "ban2_hero": ban2_hero,
                "ban2_rate": pct(source_total, total_second_order_sources),
                "source_total": source_total,
                "ban3_hero": top_ban3,
                "ban3_count": top_ban3_count,
                "ban3_rate": pct(top_ban3_count, ban3_total),
                "ban3_total": ban3_total,
                "protect2_hero": top_protect2,
                "protect2_count": top_protect2_count,
                "protect2_rate": pct(top_protect2_count, protect2_total),
                "protect2_total": protect2_total,
                "ban4_hero": top_ban4,
                "ban4_count": top_ban4_count,
                "ban4_rate": pct(top_ban4_count, ban4_total),
                "ban4_total": ban4_total,
                "sample_total": ban3_total + protect2_total + ban4_total,
            }
        )
    second_order_ban_rows.sort(key=lambda row: row["sample_total"], reverse=True)

    def top_slot_pick(slot_counts: dict, total: int) -> dict:
        sorted_rows = sorted(slot_counts.items(), key=lambda item: item[1], reverse=True)
        hero, count = sorted_rows[0] if sorted_rows else ("", 0)
        return {
            "hero": hero,
            "count": count,
            "total": total,
            "rate": pct(count, total),
        }

    total_protect1_sources = sum(
        target_data["totals"].get("source", 0)
        for target_data in protect1_influence_targets.values()
    )

    protect1_influence_rows = []
    for protect1_hero, target_data in protect1_influence_targets.items():
        source_total = target_data["totals"].get("source", 0)
        ban2_top = top_slot_pick(target_data["ban2"], target_data["totals"]["ban2"])
        ban3_top = top_slot_pick(target_data["ban3"], target_data["totals"]["ban3"])
        protect2_top = top_slot_pick(target_data["protect2"], target_data["totals"]["protect2"])
        ban4_top = top_slot_pick(target_data["ban4"], target_data["totals"]["ban4"])
        protect1_influence_rows.append(
            {
                "protect1_hero": protect1_hero,
                "protect1_rate": pct(source_total, total_protect1_sources),
                "source_total": source_total,
                "ban2": ban2_top,
                "ban3": ban3_top,
                "protect2": protect2_top,
                "ban4": ban4_top,
                "sample_total": sum(target_data["totals"].values()),
            }
        )
    protect1_influence_rows.sort(key=lambda row: row["sample_total"], reverse=True)

    total_ban1_protect1_sources = sum(
        target_data["totals"].get("source", 0)
        for target_data in ban1_protect1_route_targets.values()
    )

    most_likely_ban_route_rows = []
    for (ban1_hero, protect1_hero), target_data in ban1_protect1_route_targets.items():
        source_total = target_data["totals"].get("source", 0)
        next_nodes = []
        for slot_key, slot_label in (("ban2", "Ban 2"), ("ban3", "Ban 3"), ("protect2", "P2"), ("ban4", "Ban 4")):
            slot_total = target_data["totals"].get(slot_key, 0)
            sorted_rows = sorted(target_data[slot_key].items(), key=lambda item: item[1], reverse=True)
            hero_name, hero_count = sorted_rows[0] if sorted_rows else ("", 0)
            if hero_name:
                next_nodes.append({"hero": hero_name, "label": slot_label, "rate": pct(hero_count, slot_total)})

        if next_nodes:
            source_rate = pct(source_total, total_ban1_protect1_sources)
            most_likely_ban_route_rows.append(
                {
                    "source_nodes": [
                        {"hero": ban1_hero, "label": "Ban 1", "rate": source_rate},
                        {"hero": protect1_hero, "label": "Protect 1", "rate": source_rate},
                    ],
                    "next_nodes": next_nodes,
                    "source_total": source_total,
                    "top_rate": max(node["rate"] for node in next_nodes),
                }
            )

    most_likely_ban_route_rows.sort(key=lambda row: (row["source_total"], row["top_rate"]), reverse=True)

    overall_win_rate = pct(total_wins, total_maps)

    ban_protect_rows = []
    all_draft_heroes = set(ban_stats.keys()) | set(protect_stats.keys())
    for hero in all_draft_heroes:
        ban_count = ban_stats[hero]["count"]
        protect_count = protect_stats[hero]["count"]
        ban_rate = pct(ban_count, total_filled_bans)
        protect_rate = pct(protect_count, total_filled_protects)
        ban_win_rate = pct(ban_stats[hero]["wins"], ban_count)
        protect_win_rate = pct(protect_stats[hero]["wins"], protect_count)
        ban_delta = round(ban_win_rate - overall_win_rate, 1) if ban_count else 0.0
        protect_delta = round(protect_win_rate - overall_win_rate, 1) if protect_count else 0.0
        winrate_gap = round(protect_win_rate - ban_win_rate, 1) if ban_count and protect_count else None
        rate_gap = round(ban_rate - protect_rate, 1)
        draft_presence = round(ban_rate + protect_rate, 1)
        if rate_gap >= 5:
            leaning = "Ban leaning"
        elif rate_gap <= -5:
            leaning = "Protect leaning"
        else:
            leaning = "Balanced"

        ban_protect_rows.append(
            {
                "hero": hero,
                "ban_count": ban_count,
                "protect_count": protect_count,
                "ban_rate": ban_rate,
                "protect_rate": protect_rate,
                "ban_win_rate": ban_win_rate,
                "protect_win_rate": protect_win_rate,
                "ban_delta": ban_delta,
                "protect_delta": protect_delta,
                "winrate_gap": winrate_gap,
                "rate_gap": rate_gap,
                "draft_presence": draft_presence,
                "leaning": leaning,
            }
        )
    ban_protect_rows.sort(key=lambda r: (r["draft_presence"], abs(r["rate_gap"]), r["ban_count"] + r["protect_count"]), reverse=True)

    def calc_correlation(pairs: list[tuple[int, int]]) -> float | None:
        if len(pairs) < 2:
            return None
        xs = [pair[0] for pair in pairs]
        ys = [pair[1] for pair in pairs]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        num = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
        den_x = sum((x - mean_x) ** 2 for x in xs)
        den_y = sum((y - mean_y) ** 2 for y in ys)
        if den_x <= 0 or den_y <= 0:
            return None
        return round(num / ((den_x ** 0.5) * (den_y ** 0.5)), 2)

    ban_protect_correlation = calc_correlation(
        [(row["ban_count"], row["protect_count"]) for row in ban_protect_rows if row["ban_count"] or row["protect_count"]]
    )
    top_ban_protect = ban_protect_rows[0] if ban_protect_rows else None

    ban_diff_rows = []
    for hero in set(ban_stats.keys()) | set(enemy_ban_stats.keys()):
        our_count = ban_stats[hero]["count"]
        enemy_count = enemy_ban_stats[hero]["count"]
        our_rate = pct(our_count, total_filled_bans)
        enemy_rate = pct(enemy_count, total_enemy_filled_bans)
        rate_diff = round(our_rate - enemy_rate, 1)
        if rate_diff > 0:
            edge_label = "We ban more"
        elif rate_diff < 0:
            edge_label = "Enemy bans more"
        else:
            edge_label = "Even"

        ban_diff_rows.append(
            {
                "hero": hero,
                "our_count": our_count,
                "enemy_count": enemy_count,
                "our_rate": our_rate,
                "enemy_rate": enemy_rate,
                "rate_diff": rate_diff,
                "abs_diff": abs(rate_diff),
                "edge_label": edge_label,
            }
        )
    ban_diff_rows.sort(key=lambda r: (r["abs_diff"], r["our_count"] + r["enemy_count"]), reverse=True)

    def build_ban_position_rows(position_stats: dict, position_totals: dict) -> list[dict]:
        rows = []
        for slot_key in ban_slot_keys:
            total_for_slot = position_totals.get(slot_key, 0)
            hero_rows = []
            for hero, stats in position_stats[slot_key].items():
                hero_rows.append(
                    {
                        "hero": hero,
                        "count": stats["count"],
                        "rate": pct(stats["count"], total_for_slot),
                        "win_rate": pct(stats["wins"], stats["count"]),
                    }
                )
            hero_rows.sort(key=lambda r: (r["count"], r["rate"], r["win_rate"]), reverse=True)
            top_row = hero_rows[0] if hero_rows else None
            rows.append(
                {
                    "slot_key": slot_key,
                    "slot_label": f"Ban {slot_key[-1]}",
                    "total": total_for_slot,
                    "unique_heroes": len(hero_rows),
                    "top_hero": top_row["hero"] if top_row else "—",
                    "top_count": top_row["count"] if top_row else 0,
                    "top_rate": top_row["rate"] if top_row else 0,
                    "hero_rows": hero_rows[:3],
                }
            )
        return rows

    ban_position_rows = build_ban_position_rows(ban_position_stats, ban_position_totals)
    enemy_ban_position_rows = build_ban_position_rows(enemy_ban_position_stats, enemy_ban_position_totals)

    def add_ban_position_insights(primary_rows: list[dict], secondary_rows: list[dict]) -> None:
        secondary_lookup = {row["slot_key"]: row for row in secondary_rows}
        for row in primary_rows:
            top_hero = row.get("top_hero") or ""
            if not top_hero or top_hero == "—":
                row["insight_state"] = "even"
                row["insight_rate"] = 0
                row["insight_compare_rate"] = 0
                row["insight_hero"] = ""
                continue

            other_row = secondary_lookup.get(row["slot_key"], {})
            other_hero_rows = other_row.get("hero_rows", [])
            other_match = next((hero_row for hero_row in other_hero_rows if hero_row["hero"] == top_hero), None)
            other_rate = other_match["rate"] if other_match else 0
            rate = row.get("top_rate", 0)
            diff = round(rate - other_rate, 1)
            if diff > 0:
                state = "more"
            elif diff < 0:
                state = "less"
            else:
                state = "even"

            row["insight_state"] = state
            row["insight_rate"] = rate
            row["insight_compare_rate"] = other_rate
            row["insight_diff"] = diff
            row["insight_hero"] = top_hero

    add_ban_position_insights(ban_position_rows, enemy_ban_position_rows)
    add_ban_position_insights(enemy_ban_position_rows, ban_position_rows)

    def build_ban_phase_variation_summary(position_rows: list[dict], side_label: str) -> dict:
        early_rows = [row for row in position_rows if row.get("slot_key") in ("ban1", "ban2")]
        late_rows = [row for row in position_rows if row.get("slot_key") in ("ban3", "ban4")]

        if not early_rows or not late_rows:
            return {
                "side_label": side_label,
                "early_unique_avg": 0.0,
                "late_unique_avg": 0.0,
                "early_top_rate_avg": 0.0,
                "late_top_rate_avg": 0.0,
                "variation_unique_diff": 0.0,
                "variation_top_rate_diff": 0.0,
                "signal": "insufficient",
                "message": "Not enough ban slot data yet to evaluate Ban 1-2 vs Ban 3-4 variation.",
            }

        early_unique_avg = round(sum(row.get("unique_heroes", 0) for row in early_rows) / len(early_rows), 1)
        late_unique_avg = round(sum(row.get("unique_heroes", 0) for row in late_rows) / len(late_rows), 1)
        early_top_rate_avg = round(sum(row.get("top_rate", 0) for row in early_rows) / len(early_rows), 1)
        late_top_rate_avg = round(sum(row.get("top_rate", 0) for row in late_rows) / len(late_rows), 1)

        variation_unique_diff = round(late_unique_avg - early_unique_avg, 1)
        variation_top_rate_diff = round(early_top_rate_avg - late_top_rate_avg, 1)

        strong_variation = variation_unique_diff >= 0.5 or variation_top_rate_diff >= 8
        mild_variation = variation_unique_diff > 0 or variation_top_rate_diff > 0

        if strong_variation:
            signal = "strong"
            message = (
                f"{side_label} shows strong Ban 3-4 variation versus Ban 1-2, "
                "which often indicates team-specific targeting."
            )
        elif mild_variation:
            signal = "moderate"
            message = (
                f"{side_label} shows some extra variation in Ban 3-4 compared to Ban 1-2, "
                "suggesting partial team-specific adjustments."
            )
        else:
            signal = "low"
            message = (
                f"{side_label} has similar variation across Ban 1-2 and Ban 3-4, "
                "so bans currently look more meta-stable."
            )

        return {
            "side_label": side_label,
            "early_unique_avg": early_unique_avg,
            "late_unique_avg": late_unique_avg,
            "early_top_rate_avg": early_top_rate_avg,
            "late_top_rate_avg": late_top_rate_avg,
            "variation_unique_diff": variation_unique_diff,
            "variation_top_rate_diff": variation_top_rate_diff,
            "signal": signal,
            "message": message,
        }

    main_ban_variation = build_ban_phase_variation_summary(ban_position_rows, perspective_label)
    enemy_ban_variation = build_ban_phase_variation_summary(enemy_ban_position_rows, opponent_label)

    protect_rows = []
    for hero, stats in protect_stats.items():
        protect_rows.append(
            {
                "hero": hero,
                "count": stats["count"],
                "protect_rate": pct(stats["count"], total_filled_protects),
                "win_rate": pct(stats["wins"], stats["count"]),
            }
        )
    protect_rows.sort(key=lambda r: (r["count"], r["win_rate"]), reverse=True)

    hero_rows = []
    for hero, stats in hero_stats.items():
        hero_rows.append(
            {
                "hero": hero,
                "maps": stats["maps"],
                "win_rate": pct(stats["wins"], stats["maps"]),
                "unmirrored_maps": stats["unmirrored_maps"],
                "unmirrored_win_rate": pct(stats["unmirrored_wins"], stats["unmirrored_maps"]),
            }
        )
    hero_rows.sort(key=lambda r: (r["maps"], r["win_rate"]), reverse=True)

    map_rows = []
    for map_name, stats in map_stats.items():
        map_rows.append(
            {
                "map_name": map_name,
                "maps": stats["maps"],
                "win_rate": pct(stats["wins"], stats["maps"]),
            }
        )
    map_rows.sort(key=lambda r: (r["maps"], r["win_rate"]), reverse=True)

    # If one hero is clearly the overall most banned hero, suppress it in the
    # per-map spotlight so map-specific trends stay visible.
    dominant_ban_hero = ""
    if ban_stats:
        sorted_global_bans = sorted(
            ((hero, stats["count"]) for hero, stats in ban_stats.items()),
            key=lambda item: item[1],
            reverse=True,
        )
        if sorted_global_bans:
            top_count = sorted_global_bans[0][1]
            top_heroes = [hero for hero, count in sorted_global_bans if count == top_count]
            if len(top_heroes) == 1:
                dominant_ban_hero = top_heroes[0]

    map_draft_rows = []
    for map_name, stats in map_draft_stats.items():
        ban_totals = stats["ban_totals"]
        protect_totals = stats["protect_totals"]

        top_ban_hero = ""
        top_ban_count = 0
        if stats["ban_heroes"]:
            sorted_map_bans = sorted(
                stats["ban_heroes"].items(),
                key=lambda item: item[1],
                reverse=True,
            )
            top_ban_hero, top_ban_count = sorted_map_bans[0]
            if dominant_ban_hero and top_ban_hero == dominant_ban_hero and len(sorted_map_bans) > 1:
                top_ban_hero, top_ban_count = sorted_map_bans[1]

        top_protect_hero = ""
        top_protect_count = 0
        if stats["protect_heroes"]:
            top_protect_hero, top_protect_count = max(stats["protect_heroes"].items(), key=lambda item: item[1])

        top_ban_rate = pct(top_ban_count, ban_totals)
        top_protect_rate = pct(top_protect_count, protect_totals)
        is_skip = top_ban_hero and top_ban_count >= 2 and top_ban_rate >= 35
        if is_skip:
            recommendation = "Skip: banned too often"
        elif top_ban_hero:
            recommendation = "Playable: bans not overwhelming"
        else:
            recommendation = "No draft data"

        map_draft_rows.append(
            {
                "map_name": map_name,
                "map_image": MAP_IMAGES.get(map_name, ""),
                "top_ban_hero": top_ban_hero,
                "top_ban_count": top_ban_count,
                "top_ban_rate": top_ban_rate,
                "top_protect_hero": top_protect_hero,
                "top_protect_count": top_protect_count,
                "top_protect_rate": top_protect_rate,
                "recommendation": recommendation,
                "skip_flag": is_skip,
            }
        )
    map_draft_rows.sort(key=lambda r: (r["skip_flag"], r["top_ban_rate"], r["top_ban_count"]), reverse=True)

    hero_open_rows = []
    for hero_name, stats in hero_open_stats.items():
        open_maps = stats["open_maps"]
        played_when_open = stats["played_when_open"]
        if not open_maps or not played_when_open:
            continue

        not_played_when_open = max(0, open_maps - played_when_open)
        closed_maps = stats["closed_maps"]
        win_rate_when_open = pct(stats["open_wins"], open_maps)
        win_rate_when_open_played = pct(stats["played_wins"], played_when_open)
        open_not_played_wins = max(0, stats["open_wins"] - stats["played_wins"])
        win_rate_when_open_not_played = pct(open_not_played_wins, not_played_when_open)
        win_rate_when_closed = pct(stats["closed_wins"], closed_maps)
        open_vs_closed_delta = round(win_rate_when_open - win_rate_when_closed, 1) if closed_maps else None
        played_vs_not_played_open_delta = (
            round(win_rate_when_open_played - win_rate_when_open_not_played, 1)
            if played_when_open and not_played_when_open
            else None
        )
        open_vs_overall_delta = round(win_rate_when_open - overall_win_rate, 1)
        play_when_open_rate = pct(played_when_open, open_maps)
        total_observed_maps = open_maps + closed_maps
        ban_rate = pct(closed_maps, total_observed_maps)
        fully_open_maps = stats["fully_open_maps"]
        our_played_when_fully_open = stats["our_played_when_fully_open"]
        enemy_played_when_fully_open = stats["enemy_played_when_fully_open"]
        our_fully_open_rate = pct(our_played_when_fully_open, fully_open_maps)
        enemy_fully_open_rate = pct(enemy_played_when_fully_open, fully_open_maps)
        fully_open_play_diff = round(our_fully_open_rate - enemy_fully_open_rate, 1) if fully_open_maps else None
        if fully_open_play_diff is None:
            fully_open_edge_label = "--"
        elif fully_open_play_diff > 0:
            fully_open_edge_label = "We play more"
        elif fully_open_play_diff < 0:
            fully_open_edge_label = "Opponent plays more"
        else:
            fully_open_edge_label = "Even"
        teammate_open_counts = stats["teammate_open_counts"]
        top_teammate_name = ""
        top_teammate_count = 0
        if teammate_open_counts:
            top_teammate_name, top_teammate_count = max(
                teammate_open_counts.items(),
                key=lambda item: (item[1], item[0].lower()),
            )
        top_teammate_rate = pct(top_teammate_count, played_when_open)

        hero_open_rows.append(
            {
                "hero": _resolve_hero_transform_key(hero_name) or hero_name,
                "open_maps": open_maps,
                "open_rate": pct(open_maps, total_maps),
                "banned_maps": closed_maps,
                "played_when_open": played_when_open,
                "not_played_when_open": not_played_when_open,
                "play_when_open_rate": play_when_open_rate,
                "win_rate_when_open": win_rate_when_open,
                "win_rate_when_open_played": win_rate_when_open_played,
                "win_rate_when_open_not_played": win_rate_when_open_not_played,
                "played_vs_not_played_open_delta": played_vs_not_played_open_delta,
                "win_rate_when_closed": win_rate_when_closed,
                "win_rate_when_banned": win_rate_when_closed,
                "open_vs_closed_delta": open_vs_closed_delta,
                "open_vs_banned_delta": open_vs_closed_delta,
                "open_vs_overall_delta": open_vs_overall_delta,
                "fully_open_maps": fully_open_maps,
                "our_played_when_fully_open": our_played_when_fully_open,
                "enemy_played_when_fully_open": enemy_played_when_fully_open,
                "ban_rate": ban_rate,
                "our_fully_open_rate": our_fully_open_rate,
                "enemy_fully_open_rate": enemy_fully_open_rate,
                "fully_open_play_diff": fully_open_play_diff,
                "fully_open_edge_label": fully_open_edge_label,
                "top_teammate_name": top_teammate_name,
                "top_teammate_count": top_teammate_count,
                "top_teammate_rate": top_teammate_rate,
            }
        )
    def _open_priority(row: dict) -> tuple:
        # Primary: composite of play-rate × win-rate-when-played (both 0-100),
        # scaled so a hero always played and always winning scores 10000.
        # This surfaces "must-play and winning" heroes regardless of sample size.
        play_rate = row["play_when_open_rate"]          # 0-100
        wr_played = row["win_rate_when_open_played"]    # 0-100
        composite = play_rate * wr_played               # max 10000
        # Secondary: sample confidence (more open maps = more reliable signal)
        return (composite, row["played_when_open"], row["open_maps"])

    hero_open_rows.sort(key=_open_priority, reverse=True)

    mirror_rates = {
        "draft": {
            "samples": draft_mirror_total,
            "mirror_count": draft_soft_mirror_count,
            "mirror_rate": pct(draft_soft_mirror_count, draft_mirror_total),
        },
        "comp": {
            "samples": comp_mirror_total,
            "mirror_count": comp_soft_mirror_count,
            "mirror_rate": pct(comp_soft_mirror_count, comp_mirror_total),
        },
    }

    triple_support_count = comp_profile_stats["triple_support"]["count"]
    two_two_two_count = comp_profile_stats["two_two_two"]["count"]
    triple_tank_count = comp_profile_stats["triple_tank"]["count"]
    triple_support_rate = pct(triple_support_count, total_maps)
    two_two_two_rate = pct(two_two_two_count, total_maps)
    triple_tank_rate = pct(triple_tank_count, total_maps)
    comp_difference_rate = round(triple_support_rate - two_two_two_rate, 1)
    triple_support_win_rate = pct(comp_profile_stats["triple_support"]["wins"], triple_support_count)
    two_two_two_win_rate = pct(comp_profile_stats["two_two_two"]["wins"], two_two_two_count)
    triple_tank_win_rate = pct(comp_profile_stats["triple_tank"]["wins"], triple_tank_count)
    enemy_triple_support_count = enemy_comp_profile_stats["triple_support"]["count"]
    enemy_triple_support_rate = pct(enemy_triple_support_count, total_maps)
    enemy_triple_tank_count = enemy_comp_profile_stats["triple_tank"]["count"]
    enemy_triple_tank_rate = pct(enemy_triple_tank_count, total_maps)
    triple_support_prevalence_diff = round(triple_support_rate - enemy_triple_support_rate, 1)
    comp_winrate_difference = (
        round(triple_support_win_rate - two_two_two_win_rate, 1)
        if triple_support_count and two_two_two_count
        else None
    )

    comp_archetype_labels = {
        "triple_support": "Triple Support",
        "two_two_two": "2-2-2",
        "other": "Other / Flex",
    }
    comp_archetype_rows = []
    for profile_key in ("triple_support", "two_two_two", "other"):
        main_count = comp_profile_stats[profile_key]["count"]
        enemy_count = enemy_comp_profile_stats[profile_key]["count"]
        main_rate = pct(main_count, total_maps)
        enemy_rate = pct(enemy_count, total_maps)
        rate_diff = round(main_rate - enemy_rate, 1)
        comp_archetype_rows.append(
            {
                "profile_key": profile_key,
                "label": comp_archetype_labels.get(profile_key, profile_key.replace("_", " ").title()),
                "main_count": main_count,
                "main_rate": main_rate,
                "main_win_rate": pct(comp_profile_stats[profile_key]["wins"], main_count),
                "enemy_count": enemy_count,
                "enemy_rate": enemy_rate,
                "enemy_win_rate": pct(enemy_comp_profile_stats[profile_key]["wins"], enemy_count),
                "rate_diff": rate_diff,
            }
        )

    return {
        "summary": {
            "total_maps": total_maps,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "overall_win_rate": overall_win_rate,
            "unique_bans": len(ban_rows),
            "unique_enemy_bans": len(enemy_ban_rows),
            "unique_protects": len(protect_rows),
            "unique_heroes": len(hero_rows),
            "unique_maps": len(map_rows),
        },
        "ban_protect_summary": {
            "correlation": ban_protect_correlation,
            "top_hero": top_ban_protect["hero"] if top_ban_protect else "—",
            "top_presence": top_ban_protect["draft_presence"] if top_ban_protect else 0,
        },
        "comp_difference": {
            "triple_support_count": triple_support_count,
            "triple_support_rate": triple_support_rate,
            "triple_support_win_rate": triple_support_win_rate,
            "enemy_triple_support_count": enemy_triple_support_count,
            "enemy_triple_support_rate": enemy_triple_support_rate,
            "triple_support_prevalence_diff": triple_support_prevalence_diff,
            "triple_tank_count": triple_tank_count,
            "triple_tank_rate": triple_tank_rate,
            "triple_tank_win_rate": triple_tank_win_rate,
            "enemy_triple_tank_count": enemy_triple_tank_count,
            "enemy_triple_tank_rate": enemy_triple_tank_rate,
            "two_two_two_count": two_two_two_count,
            "two_two_two_rate": two_two_two_rate,
            "two_two_two_win_rate": two_two_two_win_rate,
            "difference_rate": comp_difference_rate,
            "winrate_difference": comp_winrate_difference,
        },
        "ban_rows": ban_rows[:12],
        "enemy_ban_rows": enemy_ban_rows[:12],
        "ban_position_rows": ban_position_rows,
        "enemy_ban_position_rows": enemy_ban_position_rows,
        "ban_phase_variation": {
            "main": main_ban_variation,
            "enemy": enemy_ban_variation,
        },
        "mirror_rates": mirror_rates,
        "comp_archetype_rows": comp_archetype_rows,
        "ban_diff_rows": ban_diff_rows[:12],
        "ban_next_rows": ban_next_rows[:12],
        "ban_to_protect_rows": ban_to_protect_rows[:12],
        "draft_route_rows": draft_route_rows[:16],
        "second_order_ban_rows": second_order_ban_rows[:12],
        "protect1_influence_rows": protect1_influence_rows[:12],
        "most_likely_ban_route_rows": most_likely_ban_route_rows[:16],
        "lead_to_ban_rows": lead_to_ban_rows[:12],
        "lead_to_protect_rows": lead_to_protect_rows[:12],
        "ban_protect_rows": ban_protect_rows[:12],
        "hero_open_rows": hero_open_rows[:16],
        "protect_rows": protect_rows[:12],
        "hero_rows": hero_rows[:12],
        "map_rows": map_rows[:12],
        "map_draft_rows": map_draft_rows[:12],
    }


def opposite_team_slot(team_slot: str) -> str:
    return "team2" if team_slot == "team1" else "team1"


def _draft_slot_label(slot_key: str) -> str:
    labels = {
        "ban1": "Ban 1",
        "protect1": "Protect 1",
        "ban2": "Ban 2",
        "ban3": "Ban 3",
        "protect2": "Protect 2",
        "ban4": "Ban 4",
    }
    return labels.get(slot_key, slot_key)


def _canonical_draft_hero(raw_hero: str) -> str:
    return normalize_hero_slot_value(raw_hero)


def _summarize_draft_phase_slot_counts(slot_counts: dict[str, dict[str, int]]) -> list[dict]:
    rows = []
    for slot_key in DRAFT_SLOT_ORDER:
        slot_totals = sum(slot_counts.get(slot_key, {}).values())
        hero_rows = [
            {
                "hero": hero,
                "count": count,
                "rate": round((count / slot_totals) * 100, 1) if slot_totals else 0,
            }
            for hero, count in sorted(slot_counts.get(slot_key, {}).items(), key=lambda item: item[1], reverse=True)
        ]
        top_row = hero_rows[0] if hero_rows else None
        rows.append(
            {
                "slot_key": slot_key,
                "slot_label": _draft_slot_label(slot_key),
                "total": slot_totals,
                "top_hero": top_row["hero"] if top_row else "—",
                "top_count": top_row["count"] if top_row else 0,
                "top_rate": top_row["rate"] if top_row else 0,
                "hero_rows": hero_rows[:3],
            }
        )
    return rows


def build_draft_phase_timeline(scrims: list[dict]) -> dict:
    def new_side_counts() -> dict[str, defaultdict[str, int]]:
        return {slot_key: defaultdict(int) for slot_key in DRAFT_SLOT_ORDER}

    aggregate = {
        "main": new_side_counts(),
        "enemy": new_side_counts(),
        "maps": 0,
    }
    per_map = defaultdict(lambda: {"main": new_side_counts(), "enemy": new_side_counts(), "maps": 0})

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            map_name = (map_entry.get("map_name", "") or "").strip() or "Unknown Map"
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            draft = map_entry.get("draft", {})
            if not isinstance(draft, dict):
                continue

            our_draft = draft.get(our_team_slot, {})
            enemy_draft = draft.get(opposite_team_slot(our_team_slot), {})
            if not isinstance(our_draft, dict):
                our_draft = {}
            if not isinstance(enemy_draft, dict):
                enemy_draft = {}

            aggregate["maps"] += 1
            per_map[map_name]["maps"] += 1

            for side_key, side_draft in (("main", our_draft), ("enemy", enemy_draft)):
                for slot_key in DRAFT_SLOT_ORDER:
                    hero_name = _canonical_draft_hero(side_draft.get(slot_key, ""))
                    if not hero_name:
                        continue
                    aggregate[side_key][slot_key][hero_name] += 1
                    per_map[map_name][side_key][slot_key][hero_name] += 1

    map_rows = []
    for map_name, payload in per_map.items():
        map_rows.append(
            {
                "map_name": map_name,
                "maps": payload["maps"],
                "main_rows": _summarize_draft_phase_slot_counts(payload["main"]),
                "enemy_rows": _summarize_draft_phase_slot_counts(payload["enemy"]),
            }
        )
    map_rows.sort(key=lambda row: (row["maps"], row["map_name"]), reverse=True)

    return {
        "aggregate": {
            "maps": aggregate["maps"],
            "main_rows": _summarize_draft_phase_slot_counts(aggregate["main"]),
            "enemy_rows": _summarize_draft_phase_slot_counts(aggregate["enemy"]),
        },
        "maps": map_rows,
    }


def build_draft_phase_map_comparison_rows(
    timeline_data: dict,
    compare_map_a: str,
    compare_map_b: str,
) -> list[dict]:
    map_lookup = {row["map_name"]: row for row in timeline_data.get("maps", [])}
    map_a = map_lookup.get(compare_map_a)
    map_b = map_lookup.get(compare_map_b)
    if not map_a or not map_b:
        return []

    map_a_main = {row["slot_key"]: row for row in map_a["main_rows"]}
    map_a_enemy = {row["slot_key"]: row for row in map_a["enemy_rows"]}
    map_b_main = {row["slot_key"]: row for row in map_b["main_rows"]}
    map_b_enemy = {row["slot_key"]: row for row in map_b["enemy_rows"]}

    rows = []
    for slot_key in DRAFT_SLOT_ORDER:
        rows.append(
            {
                "slot_key": slot_key,
                "slot_label": _draft_slot_label(slot_key),
                "map_a_main": map_a_main.get(slot_key, {}),
                "map_a_enemy": map_a_enemy.get(slot_key, {}),
                "map_b_main": map_b_main.get(slot_key, {}),
                "map_b_enemy": map_b_enemy.get(slot_key, {}),
            }
        )
    return rows


def filter_team_scrims_for_enemy(team_scrims: list[dict], enemy_team_id: int | None, enemy_team_name: str = "") -> list[dict]:
    if not enemy_team_id and not enemy_team_name:
        return team_scrims

    filtered = []
    enemy_name_lower = (enemy_team_name or "").strip().lower()
    for scrim in team_scrims:
        # --- ID matching ---
        if enemy_team_id:
            # Legacy enemy_team_id field (enemy_teams table).
            scrim_enemy_id = scrim.get("enemy_team_id")
            if scrim_enemy_id and scrim_enemy_id == enemy_team_id:
                filtered.append(scrim)
                continue

            # Scrims between two registered teams store IDs in team1_id / team2_id.
            # Our slot is already resolved; the opponent is on the other side.
            our_slot = scrim.get("team_slot", "team1")
            if our_slot == "team1":
                opp_id = scrim.get("team2_id")
            else:
                opp_id = scrim.get("team1_id")
            if opp_id and opp_id == enemy_team_id:
                filtered.append(scrim)
                continue

        # --- Name matching fallback ---
        scrim_enemy_name = (scrim.get("enemy_team") or scrim.get("opponent") or "").strip().lower()
        if not scrim_enemy_name:
            # Try team1_name / team2_name based on slot
            our_slot = scrim.get("team_slot", "team1")
            if our_slot == "team1":
                scrim_enemy_name = (scrim.get("team2_name") or "").strip().lower()
            else:
                scrim_enemy_name = (scrim.get("team1_name") or "").strip().lower()
        if enemy_name_lower and scrim_enemy_name == enemy_name_lower:
            filtered.append(scrim)

    return filtered


def build_prep_expected_comp_plan(prep_scrims: list[dict], team_players: list[sqlite3.Row | dict], prep_analytics: dict, all_scrims: list[dict] | None = None) -> dict:
    pair_counts = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    hero_counts = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    comp_variant_counts = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})

    players = [
        {
            "name": (row["name"] if isinstance(row, sqlite3.Row) else row.get("name", "")).strip(),
            "role": (row["role"] if isinstance(row, sqlite3.Row) else row.get("role", "")).strip(),
            "main_hero": (row["main_hero"] if isinstance(row, sqlite3.Row) else row.get("main_hero", "")).strip(),
        }
        for row in team_players
        if (row["name"] if isinstance(row, sqlite3.Row) else row.get("name", "")).strip()
    ]
    roster_name_lookup = {
        player["name"].lower(): player["name"]
        for player in players
        if player.get("name")
    }

    player_by_main_hero = defaultdict(list)
    for player in players:
        main_hero = _canonical_draft_hero(player["main_hero"])
        if main_hero:
            player_by_main_hero[main_hero].append(player)

    for scrim in prep_scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            result = get_map_outcome_for_slot(map_entry, our_team_slot)

            map_pairs: list[tuple[str, str]] = []
            map_heroes: list[str] = []
            largest_lineup: list[str] = []

            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue

                lineup = []
                for slot in section.get(our_team_slot, []):
                    if not isinstance(slot, dict):
                        continue
                    hero_name = _canonical_draft_hero(slot.get("hero", ""))
                    if not hero_name:
                        continue

                    lineup.append(hero_name)
                    map_heroes.append(hero_name)

                    player_name = (slot.get("player", "") or "").strip()
                    if player_name:
                        roster_player_name = roster_name_lookup.get(player_name.lower())
                        if roster_player_name:
                            map_pairs.append((hero_name, roster_player_name))

                if len(lineup) > len(largest_lineup):
                    largest_lineup = lineup

            for hero_name in map_heroes:
                hero_counts[hero_name]["count"] += 1
                if result == "Win":
                    hero_counts[hero_name]["wins"] += 1
                elif result == "Loss":
                    hero_counts[hero_name]["losses"] += 1

            for hero_name, player_name in map_pairs:
                pair_counts[(hero_name, player_name)]["count"] += 1
                if result == "Win":
                    pair_counts[(hero_name, player_name)]["wins"] += 1
                elif result == "Loss":
                    pair_counts[(hero_name, player_name)]["losses"] += 1

            if largest_lineup:
                lineup_key = tuple(sorted(largest_lineup))
                comp_variant_counts[lineup_key]["count"] += 1
                if result == "Win":
                    comp_variant_counts[lineup_key]["wins"] += 1
                elif result == "Loss":
                    comp_variant_counts[lineup_key]["losses"] += 1

    # Build a full pair_counts from ALL scrims (not just vs this enemy) so that
    # player capability lookups have enough history even when enemy-filtered data is thin.
    full_pair_counts: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    for _scrim in (all_scrims or prep_scrims):
        for _map in _scrim.get("maps", []):
            _our_slot = _map.get("our_team_slot", "team1")
            if _our_slot not in TEAM_SLOTS:
                _our_slot = "team1"
            for _section in _map.get("comp", []):
                if not isinstance(_section, dict):
                    continue
                for _slot in _section.get(_our_slot, []):
                    if not isinstance(_slot, dict):
                        continue
                    _hero = _canonical_draft_hero(_slot.get("hero", ""))
                    _player = (_slot.get("player", "") or "").strip()
                    if _hero and _player:
                        _roster_pname = roster_name_lookup.get(_player.lower())
                        if _roster_pname:
                            full_pair_counts[(_hero, _roster_pname)]["count"] += 1

    # Determine each player's main heroes (top played, min 2 maps, up to 3).
    # Falls back to single most-played if none meet the 2-map threshold.
    player_main_heroes: dict[str, list[str]] = {}
    for player_obj in players:
        pname = player_obj["name"]
        player_hero_counts = sorted(
            [
                (hero_name, stats["count"])
                for (hero_name, player_name), stats in pair_counts.items()
                if player_name == pname
            ],
            key=lambda x: x[1],
            reverse=True,
        )
        if player_hero_counts:
            mains = [h for h, c in player_hero_counts if c >= 2][:3]
            if not mains:
                mains = [player_hero_counts[0][0]]
            player_main_heroes[pname] = mains
        elif player_obj.get("main_hero"):
            player_main_heroes[pname] = [_canonical_draft_hero(player_obj["main_hero"])]

    # Second pass: for each map where any of a player's main heroes was enemy-banned,
    # track what that player actually switched to and the outcome.
    # Key is (player_name, banned_main_hero).
    player_ban_pivot_counts: dict[tuple, dict] = {
        (pname, main_h): defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
        for pname, mains in player_main_heroes.items()
        for main_h in mains
    }
    player_main_ban_total: dict[tuple, int] = defaultdict(int)

    for scrim in prep_scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            pivot_result = get_map_outcome_for_slot(map_entry, our_team_slot)
            enemy_slot = "team2" if our_team_slot == "team1" else "team1"
            draft = map_entry.get("draft", {})
            enemy_draft = draft.get(enemy_slot, {}) if isinstance(draft, dict) else {}
            enemy_bans = {
                _canonical_draft_hero(v)
                for k, v in enemy_draft.items()
                if "ban" in k and _canonical_draft_hero(v)
            }

            # Build map: roster player -> heroes they played this map
            player_heroes_this_map: dict[str, list] = defaultdict(list)
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(our_team_slot, []):
                    if not isinstance(slot, dict):
                        continue
                    h = _canonical_draft_hero(slot.get("hero", ""))
                    p = (slot.get("player", "") or "").strip()
                    if h and p:
                        roster_pname = roster_name_lookup.get(p.lower())
                        if roster_pname:
                            player_heroes_this_map[roster_pname].append(h)

            for pname, mains in player_main_heroes.items():
                for main_h in mains:
                    if main_h in enemy_bans:
                        player_main_ban_total[(pname, main_h)] += 1
                        for h in player_heroes_this_map.get(pname, []):
                            if h != main_h:
                                player_ban_pivot_counts[(pname, main_h)][h]["count"] += 1
                                if pivot_result == "Win":
                                    player_ban_pivot_counts[(pname, main_h)][h]["wins"] += 1
                                elif pivot_result == "Loss":
                                    player_ban_pivot_counts[(pname, main_h)][h]["losses"] += 1

    player_pivot_rows: list[dict] = []
    for pname, mains in player_main_heroes.items():
        for main_h in mains:
            main_stats = pair_counts.get((main_h, pname), {"count": 0, "wins": 0, "losses": 0})
            main_maps = main_stats["count"]
            if main_maps == 0:
                continue
            main_wr = round((main_stats["wins"] / main_maps) * 100, 1)
            banned_maps = player_main_ban_total.get((pname, main_h), 0)
            pivot_counts = player_ban_pivot_counts.get((pname, main_h), {})
            pivot_hero = ""
            pivot_maps = 0
            pivot_wr = 0.0
            if pivot_counts:
                top_key, top_stats = max(pivot_counts.items(), key=lambda x: (x[1]["count"], x[1]["wins"]))
                pivot_hero = top_key
                pivot_maps = top_stats["count"]
                pivot_wr = round((top_stats["wins"] / pivot_maps) * 100, 1) if pivot_maps else 0.0
            player_pivot_rows.append({
                "player_name": pname,
                "main_hero": main_h,
                "main_hero_maps": main_maps,
                "main_hero_win_rate": main_wr,
                "banned_maps": banned_maps,
                "pivot_hero": pivot_hero,
                "pivot_maps": pivot_maps,
                "pivot_win_rate": pivot_wr,
            })
    # Sort by player name, then by main hero play count descending within each player
    player_pivot_rows.sort(key=lambda r: (r["player_name"].lower(), -r["main_hero_maps"]))

    def choose_player_for_hero(hero_name: str, used_names: set[str] | None = None) -> dict:
        used_names = used_names or set()
        candidates = [
            (player_name, stats)
            for (pair_hero, player_name), stats in pair_counts.items()
            if pair_hero == hero_name and player_name not in used_names
        ]
        candidates.sort(key=lambda row: (row[1]["count"], row[1]["wins"]), reverse=True)
        if candidates:
            top_name, top_stats = candidates[0]
            return {
                "name": top_name,
                "maps": top_stats["count"],
                "win_rate": round((top_stats["wins"] / top_stats["count"]) * 100, 1) if top_stats["count"] else 0,
                "source": "history",
            }

        # Fallback: check all-time pair counts (not enemy-filtered) for a richer history
        full_candidates = [
            (player_name, stats)
            for (pair_hero, player_name), stats in full_pair_counts.items()
            if pair_hero == hero_name and player_name not in used_names
        ]
        full_candidates.sort(key=lambda row: row[1]["count"], reverse=True)
        if full_candidates:
            top_name, top_stats = full_candidates[0]
            return {
                "name": top_name,
                "maps": top_stats["count"],
                "win_rate": 0,
                "source": "history_full",
            }

        hero_main_candidates = [player for player in player_by_main_hero.get(hero_name, []) if player["name"] not in used_names]
        if hero_main_candidates:
            pick = hero_main_candidates[0]
            return {
                "name": pick["name"],
                "maps": 0,
                "win_rate": 0,
                "source": "main_hero",
            }

        role_name = _hero_role(hero_name)
        role_candidates = [
            player for player in players
            if player["name"] not in used_names and role_name and player["role"].lower() == role_name.lower()
        ]
        if role_candidates:
            pick = role_candidates[0]
            return {
                "name": pick["name"],
                "maps": 0,
                "win_rate": 0,
                "source": "role_fit",
            }

        fallback = next((player for player in players if player["name"] not in used_names), None)
        if fallback is not None:
            return {
                "name": fallback["name"],
                "maps": 0,
                "win_rate": 0,
                "source": "fallback",
            }

        return {
            "name": "TBD",
            "maps": 0,
            "win_rate": 0,
            "source": "unassigned",
        }

    expected_hero_pool = [row["hero"] for row in prep_analytics.get("hero_rows", []) if row.get("hero")]
    if not expected_hero_pool:
        expected_hero_pool = [hero for hero, _stats in sorted(hero_counts.items(), key=lambda item: item[1]["count"], reverse=True)]
    expected_hero_pool = expected_hero_pool[:6]

    # Enforce minimum 2 per role (2 Vanguard, 2 Duelist, 2 Strategist)
    # unless triple support is very prominent (>35% of comp appearances)
    triple_support_appearances = sum(
        s["count"]
        for comp_key, s in comp_variant_counts.items()
        if sum(1 for h in comp_key if _hero_role(h) == "Strategist") >= 3
    )
    total_comp_appearances = sum(s["count"] for s in comp_variant_counts.values())
    triple_support_prominent = (
        total_comp_appearances > 0
        and (triple_support_appearances / total_comp_appearances) > 0.35
    )
    role_mins = {"Vanguard": 2, "Duelist": 2, "Strategist": 2}
    if triple_support_prominent:
        role_mins["Vanguard"] = 1

    pool_set = {_canonical_draft_hero(h) for h in expected_hero_pool}
    pool_by_role: dict[str, list[str]] = {}
    for h in expected_hero_pool:
        pool_by_role.setdefault(_hero_role(h), []).append(h)

    for role, min_needed in role_mins.items():
        deficit = min_needed - len(pool_by_role.get(role, []))
        if deficit > 0:
            role_candidates = sorted(
                [
                    (h, s["count"])
                    for h, s in hero_counts.items()
                    if _hero_role(h) == role and _canonical_draft_hero(h) not in pool_set
                ],
                key=lambda x: x[1],
                reverse=True,
            )
            for hero, _ in role_candidates[:deficit]:
                expected_hero_pool.append(hero)
                pool_set.add(_canonical_draft_hero(hero))
                pool_by_role.setdefault(role, []).append(hero)

    # Cap the pool to 6 heroes. If role enforcement caused it to exceed 6,
    # trim excess heroes from the most over-represented roles.
    while len(expected_hero_pool) > 6:
        role_counts: dict[str, int] = {}
        for h in expected_hero_pool:
            r = _hero_role(h)
            role_counts[r] = role_counts.get(r, 0) + 1
        # Remove the last hero whose role exceeds its minimum
        trimmed = False
        for i in range(len(expected_hero_pool) - 1, -1, -1):
            r = _hero_role(expected_hero_pool[i])
            if role_counts.get(r, 0) > role_mins.get(r, 0):
                del expected_hero_pool[i]
                trimmed = True
                break
        if not trimmed:
            # All roles are at or below their minimums; just drop the last entry
            expected_hero_pool.pop()

    expected_core = []
    used_names = set()
    for hero_name in expected_hero_pool:
        hero_stats = hero_counts.get(hero_name, {"count": 0, "wins": 0, "losses": 0})
        assignment = choose_player_for_hero(hero_name, used_names)
        if assignment.get("name") and assignment["name"] != "TBD":
            used_names.add(assignment["name"])

        expected_core.append(
            {
                "hero": hero_name,
                "role": _hero_role(hero_name),
                "maps": hero_stats["count"],
                "win_rate": round((hero_stats["wins"] / hero_stats["count"]) * 100, 1) if hero_stats["count"] else 0,
                "player": assignment,
            }
        )

    # Build lenient 5-hero core combos: groups all comps that share 5 heroes,
    # tolerating 1-hero flex pick variation.  Also track what the 6th (flex) pick
    # was across all those maps so we can surface "or <alt>" suggestions.
    combo5_counts: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    combo5_flex_counts: dict[tuple, dict] = defaultdict(lambda: defaultdict(int))

    for heroes, stats in comp_variant_counts.items():
        if len(heroes) < 5:
            continue
        for combo_key in combinations(heroes, 5):
            combo5_counts[combo_key]["count"] += stats["count"]
            combo5_counts[combo_key]["wins"] += stats["wins"]
            combo5_counts[combo_key]["losses"] += stats["losses"]
            if len(heroes) == 6:
                flex_heroes = set(heroes) - set(combo_key)
                for flex_h in flex_heroes:
                    combo5_flex_counts[combo_key][flex_h] += stats["count"]

    expected_comp_variants = []
    seen_comp_sets: list[frozenset] = []
    # Sort primarily by sample size; win rate only breaks ties between equally-played cores.
    # A comp with 8 maps at 50% WR is more reliable than one with 2 maps at 100% WR.
    sorted_5combos = sorted(
        combo5_counts.items(),
        key=lambda row: (
            row[1]["count"],
            round((row[1]["wins"] / row[1]["count"]) * 100) if row[1]["count"] else 0,
        ),
        reverse=True,
    )
    for core_heroes, stats in sorted_5combos:
        if len(expected_comp_variants) >= 3:
            break
        # Skip if this core overlaps heavily (≥4 shared heroes) with an already-shown comp.
        core_set = frozenset(core_heroes)
        if any(len(core_set & s) >= 4 for s in seen_comp_sets):
            continue
        seen_comp_sets.append(core_set)

        used_variant_names = set()
        slots = []
        for hero_name in core_heroes:
            assignment = choose_player_for_hero(hero_name, used_variant_names)
            if assignment.get("name") and assignment["name"] != "TBD":
                used_variant_names.add(assignment["name"])
            slots.append(
                {
                    "hero": hero_name,
                    "role": _hero_role(hero_name),
                    "player": assignment,
                }
            )

        flex_sorted = sorted(combo5_flex_counts[core_heroes].items(), key=lambda x: x[1], reverse=True)
        flex_hero = flex_sorted[0][0] if flex_sorted else ""
        flex_alt = flex_sorted[1][0] if len(flex_sorted) > 1 else ""
        flex_assignment = choose_player_for_hero(flex_hero, set(used_variant_names)) if flex_hero else {}
        flex_alt_assignment = choose_player_for_hero(flex_alt, set()) if flex_alt else {}

        expected_comp_variants.append(
            {
                "label": f"Expected Comp {len(expected_comp_variants) + 1}",
                "heroes": slots,
                "flex_hero": flex_hero,
                "flex_hero_player": flex_assignment,
                "flex_alt": flex_alt,
                "flex_alt_player": flex_alt_assignment,
                "maps": stats["count"],
                "win_rate": round((stats["wins"] / stats["count"]) * 100, 1) if stats["count"] else 0,
            }
        )

    combo4_counts = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    combo4_flex_counts: dict[tuple, dict] = defaultdict(lambda: defaultdict(int))
    for heroes, stats in comp_variant_counts.items():
        if len(heroes) < 4:
            continue
        for combo_key in combinations(heroes, 4):
            combo4_counts[combo_key]["count"] += stats["count"]
            combo4_counts[combo_key]["wins"] += stats["wins"]
            combo4_counts[combo_key]["losses"] += stats["losses"]
            flex_heroes = set(heroes) - set(combo_key)
            for flex_h in flex_heroes:
                combo4_flex_counts[combo_key][flex_h] += stats["count"]

    four_hero_combos = []
    seen_combo4_sets: list[frozenset] = []
    sorted_combo4 = sorted(
        combo4_counts.items(),
        key=lambda row: (
            row[1]["count"],
            round((row[1]["wins"] / row[1]["count"]) * 100) if row[1]["count"] else 0,
        ),
        reverse=True,
    )
    for heroes, stats in sorted_combo4:
        if len(four_hero_combos) >= 6:
            break
        combo_set = frozenset(heroes)
        # Skip if 2+ heroes overlap with an already-shown combo (keeps cores mostly distinct)
        if any(len(combo_set & s) >= 2 for s in seen_combo4_sets):
            continue
        seen_combo4_sets.append(combo_set)
        flex_sorted = sorted(combo4_flex_counts[heroes].items(), key=lambda x: x[1], reverse=True)
        flex_picks = [h for h, _ in flex_sorted[:2]]
        four_hero_combos.append(
            {
                "heroes": list(heroes),
                "flex": flex_picks,
                "maps": stats["count"],
                "win_rate": round((stats["wins"] / stats["count"]) * 100, 1) if stats["count"] else 0,
            }
        )

    top_enemy_bans = prep_analytics.get("enemy_ban_rows", [])[:4]
    suggested_adjustments = []
    core_hero_keys = {_canonical_draft_hero(item["hero"]) for item in expected_core}
    used_replacement_keys: set[str] = set()

    for row in top_enemy_bans:
        banned_hero = row.get("hero", "")
        banned_key = _canonical_draft_hero(banned_hero)

        # Who in our expected core plays this hero (if anyone)?
        impacted_slot = next(
            (item for item in expected_core if _canonical_draft_hero(item["hero"]) == banned_key),
            None,
        )
        impacted_player_name = impacted_slot.get("player", {}).get("name", "") if impacted_slot else ""

        banned_role = _hero_role(banned_key)

        replacement_hero = ""
        replacement_player_name = ""

        # Only suggest a swap if the banned hero is actually in our expected core.
        # If the enemy bans something we don't run, there's nothing to swap.
        if impacted_slot is not None:
            # Primary: what did this player actually play on maps where the banned hero was banned?
            # player_ban_pivot_counts[(player, banned_hero)] → {hero: {count, wins, losses}}
            if impacted_player_name:
                ban_pivot = player_ban_pivot_counts.get((impacted_player_name, banned_key), {})
                ban_pivot_alts = sorted(
                    [
                        (hero_name, stats)
                        for hero_name, stats in ban_pivot.items()
                        if _canonical_draft_hero(hero_name) not in core_hero_keys
                        and _canonical_draft_hero(hero_name) not in used_replacement_keys
                        and _canonical_draft_hero(hero_name) != banned_key
                    ],
                    key=lambda x: (x[1]["count"], x[1]["wins"]),
                    reverse=True,
                )
                if ban_pivot_alts:
                    # Best case: we saw this player swap to something when banned.
                    replacement_hero = ban_pivot_alts[0][0]
                    replacement_player_name = impacted_player_name
                    used_replacement_keys.add(_canonical_draft_hero(replacement_hero))
                else:
                    # Fallback: any same-role hero the player has played historically.
                    # Prefer enemy-filtered pair_counts; supplement with full_pair_counts
                    # so thin enemy samples don't leave players with no suggestion.
                    _capability_counts = full_pair_counts if full_pair_counts else pair_counts
                    player_role_alts = sorted(
                        [
                            (hero_name, stats)
                            for (hero_name, player_name), stats in _capability_counts.items()
                            if player_name == impacted_player_name
                            and _hero_role(hero_name) == banned_role
                            and _canonical_draft_hero(hero_name) not in core_hero_keys
                            and _canonical_draft_hero(hero_name) not in used_replacement_keys
                            and _canonical_draft_hero(hero_name) != banned_key
                        ],
                        key=lambda x: (x[1]["count"], x[1]["wins"]),
                        reverse=True,
                    )
                    if player_role_alts:
                        replacement_hero = player_role_alts[0][0]
                        replacement_player_name = impacted_player_name
                        used_replacement_keys.add(_canonical_draft_hero(replacement_hero))
                    else:
                        # No play history for this player on other same-role heroes.
                        # Check their declared main_hero in the roster as a candidate.
                        roster_player = next(
                            (p for p in players if p["name"] == impacted_player_name),
                            None,
                        )
                        roster_main = _canonical_draft_hero(roster_player["main_hero"]) if roster_player else ""
                        if (
                            roster_main
                            and roster_main != banned_key
                            and _hero_role(roster_main) == banned_role
                            and roster_main not in core_hero_keys
                            and roster_main not in used_replacement_keys
                        ):
                            replacement_hero = roster_main
                            replacement_player_name = impacted_player_name
                            used_replacement_keys.add(roster_main)
                        else:
                            # No history and no viable main_hero — still keep the same player.
                            # They're the one who needs to adapt; we just have no hero suggestion.
                            replacement_player_name = impacted_player_name

            # Fallback: only if we still have no player identified at all, search any
            # player who has played a same-role hero not already in core.
            if not replacement_player_name:
                role_candidates = sorted(
                    [
                        (h, s)
                        for h, s in hero_counts.items()
                        if _hero_role(h) == banned_role
                        and _canonical_draft_hero(h) not in core_hero_keys
                        and _canonical_draft_hero(h) not in used_replacement_keys
                        and _canonical_draft_hero(h) != banned_key
                    ],
                    key=lambda x: (x[1]["count"], x[1]["wins"]),
                    reverse=True,
                )
                if role_candidates:
                    replacement_hero = role_candidates[0][0]
                    used_replacement_keys.add(_canonical_draft_hero(replacement_hero))
                    replace_assign = choose_player_for_hero(replacement_hero)
                    replacement_player_name = replace_assign.get("name", "")

        suggested_adjustments.append(
            {
                "banned_hero": banned_hero,
                "ban_rate": row.get("ban_rate", 0),
                "impacted_player_name": impacted_player_name,
                "replacement_hero": replacement_hero,
                "replacement_player_name": replacement_player_name,
            }
        )

    hero_player_differences = []
    hero_player_rows: dict[str, list[dict]] = defaultdict(list)
    for (hero_name, player_name), stats in pair_counts.items():
        count = int(stats.get("count", 0) or 0)
        if not hero_name or not player_name or count <= 0:
            continue
        wins = int(stats.get("wins", 0) or 0)
        losses = int(stats.get("losses", 0) or 0)
        decided = wins + losses
        hero_player_rows[hero_name].append(
            {
                "player_name": player_name,
                "maps": count,
                "wins": wins,
                "losses": losses,
                "decided_maps": decided,
                "win_rate": round((wins / decided) * 100, 1) if decided else 0,
            }
        )

    for hero_name, rows in hero_player_rows.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda row: (row["maps"], row["wins"]), reverse=True)
        best = rows[0]
        alt = rows[1]
        hero_player_differences.append(
            {
                "hero": hero_name,
                "best_player": best,
                "alt_player": alt,
                "win_rate_diff": round(best["win_rate"] - alt["win_rate"], 1),
                "sample_total": best["maps"] + alt["maps"],
                "all_players": rows,
            }
        )

    hero_player_differences.sort(
        key=lambda row: (abs(row["win_rate_diff"]), row["sample_total"]),
        reverse=True,
    )

    return {
        "expected_core": expected_core,
        "expected_comp_variants": expected_comp_variants,
        "four_hero_combos": four_hero_combos,
        "suggested_adjustments": suggested_adjustments,
        "hero_player_differences": hero_player_differences[:12],
        "player_pivot_rows": player_pivot_rows,
    }


def build_team_prep_context(
    *,
    team_scrims: list[dict],
    team_players: list[sqlite3.Row | dict],
    enemy_teams: list[dict],
    selected_enemy_id_raw: str,
    compare_map_a_raw: str,
    compare_map_b_raw: str,
) -> dict:
    roster_player_names = [
        (row["name"] if isinstance(row, sqlite3.Row) else row.get("name", ""))
        for row in team_players
        if (row["name"] if isinstance(row, sqlite3.Row) else row.get("name", "")).strip()
    ]
    enemy_lookup = {str(enemy["id"]): enemy for enemy in enemy_teams}
    selected_enemy_id = selected_enemy_id_raw.strip()
    selected_enemy = enemy_lookup.get(selected_enemy_id)

    prep_scrims = team_scrims
    selected_enemy_name = ""
    if selected_enemy is not None:
        selected_enemy_name = selected_enemy["name"]
        prep_scrims = filter_team_scrims_for_enemy(team_scrims, int(selected_enemy_id), selected_enemy_name)

    prep_analytics = build_scrim_analytics(prep_scrims, roster_player_names=roster_player_names)
    draft_phase_timeline = build_draft_phase_timeline(prep_scrims)
    prep_expected_plan = build_prep_expected_comp_plan(prep_scrims, team_players, prep_analytics, all_scrims=team_scrims)

    compare_map_options = [row["map_name"] for row in draft_phase_timeline.get("maps", [])]
    compare_map_a = (compare_map_a_raw or "").strip()
    if compare_map_a not in compare_map_options:
        compare_map_a = compare_map_options[0] if compare_map_options else ""

    remaining_compare_maps = [map_name for map_name in compare_map_options if map_name != compare_map_a]
    compare_map_b = (compare_map_b_raw or "").strip()
    if compare_map_b not in remaining_compare_maps:
        compare_map_b = remaining_compare_maps[0] if remaining_compare_maps else ""

    compare_map_rows = build_draft_phase_map_comparison_rows(
        draft_phase_timeline,
        compare_map_a,
        compare_map_b,
    )

    return {
        "prep_analytics": prep_analytics,
        "prep_scrim_count": len(prep_scrims),
        "selected_prep_enemy_id": selected_enemy_id,
        "selected_prep_enemy_name": selected_enemy_name,
        "compare_map_options": compare_map_options,
        "compare_map_a": compare_map_a,
        "compare_map_b": compare_map_b,
        "compare_map_rows": compare_map_rows,
        "draft_phase_timeline": draft_phase_timeline,
        "prep_expected_plan": prep_expected_plan,
    }


def _sanitize_simulator_draft_slots(raw_slots: dict | None) -> dict[str, str]:
    cleaned = {slot_name: "" for slot_name in SIMULATOR_SLOT_ORDER}
    if not isinstance(raw_slots, dict):
        return cleaned

    for slot_name in SIMULATOR_SLOT_ORDER:
        cleaned[slot_name] = _canonical_draft_hero(raw_slots.get(slot_name, ""))
    return cleaned


def _sanitize_one_sided_concept_slots(raw_slots: dict | None) -> dict[str, str]:
    cleaned = {slot_name: "" for slot_name in CONCEPT_ONE_SIDED_SLOT_ORDER}
    if not isinstance(raw_slots, dict):
        return cleaned

    for slot_name in CONCEPT_ONE_SIDED_SLOT_ORDER:
        raw_value = (raw_slots.get(slot_name, "") or "").strip()
        if not raw_value:
            continue

        tokens = []
        seen = set()
        for token in re.split(r"[/|,]", raw_value):
            canonical = _canonical_draft_hero(token)
            if not canonical:
                continue
            canonical_key = canonical.lower()
            if canonical_key in seen:
                continue
            tokens.append(canonical)
            seen.add(canonical_key)
            if len(tokens) >= 2:
                break

        cleaned[slot_name] = "/".join(tokens)

    return cleaned


def build_draft_predictor(scrims: list[dict], raw_inputs: dict[str, str]) -> dict:
    cleaned_inputs = {
        field_key: (raw_inputs.get(field_key, "") or "").strip()
        for field_key in PREDICTOR_INPUT_ORDER
    }
    normalized_inputs = {
        field_key: _canonical_draft_hero(cleaned_inputs[field_key])
        for field_key in PREDICTOR_INPUT_ORDER
    }

    next_targets = []
    for group in PREDICTOR_GROUPS:
        missing = [item for item in group if not normalized_inputs[item[2]]]
        if missing:
            next_targets = missing
            break

    if not next_targets:
        return {
            "inputs": cleaned_inputs,
            "matching_maps": 0,
            "targets": [],
            "status": "complete",
        }

    matching_maps = 0
    target_counts = {
        field_key: defaultdict(int)
        for _, _, field_key in next_targets
    }
    comp_counts = {
        "team1": defaultdict(int),
        "team2": defaultdict(int),
    }

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            draft = map_entry.get("draft", {})
            if not isinstance(draft, dict):
                continue

            map_values = {}
            for group in PREDICTOR_GROUPS:
                for team_slot, slot_key, field_key in group:
                    team_draft = draft.get(team_slot, {})
                    if not isinstance(team_draft, dict):
                        team_draft = {}
                    map_values[field_key] = _canonical_draft_hero(team_draft.get(slot_key, ""))

            if any(
                normalized_inputs[field_key] and map_values.get(field_key, "") != normalized_inputs[field_key]
                for field_key in PREDICTOR_INPUT_ORDER
            ):
                continue

            matching_maps += 1
            for _, _, field_key in next_targets:
                hero = map_values.get(field_key, "")
                if hero:
                    target_counts[field_key][hero] += 1

            comp_sections = map_entry.get("comp", [])
            if isinstance(comp_sections, list):
                for team_slot in TEAM_SLOTS:
                    richest_comp: list[str] = []
                    for section in comp_sections:
                        if not isinstance(section, dict):
                            continue
                        lineup = section.get(team_slot, [])
                        if not isinstance(lineup, list):
                            continue

                        heroes = []
                        for slot in lineup:
                            if not isinstance(slot, dict):
                                continue
                            hero_name = _canonical_draft_hero(slot.get("hero", ""))
                            if hero_name:
                                heroes.append(hero_name)

                        if len(heroes) > len(richest_comp):
                            richest_comp = heroes

                    if richest_comp:
                        comp_counts[team_slot][tuple(richest_comp)] += 1

    target_rows = []
    for team_slot, slot_key, field_key in next_targets:
        options = sorted(target_counts[field_key].items(), key=lambda item: item[1], reverse=True)
        target_rows.append(
            {
                "field_key": field_key,
                "team_label": "Team 1" if team_slot == "team1" else "Team 2",
                "slot_label": _draft_slot_label(slot_key),
                "options": [
                    {
                        "hero": hero,
                        "count": count,
                        "rate": round((count / matching_maps) * 100, 1) if matching_maps else 0,
                    }
                    for hero, count in options[:8]
                ],
            }
        )

    likely_comps = []
    for team_slot in TEAM_SLOTS:
        comp_options = sorted(comp_counts[team_slot].items(), key=lambda item: item[1], reverse=True)
        if not comp_options:
            continue

        top_comp, top_count = comp_options[0]
        likely_comps.append(
            {
                "team_key": team_slot,
                "team_label": "Team 1" if team_slot == "team1" else "Team 2",
                "heroes": list(top_comp),
                "count": top_count,
                "rate": round((top_count / matching_maps) * 100, 1) if matching_maps else 0,
            }
        )

    return {
        "inputs": cleaned_inputs,
        "matching_maps": matching_maps,
        "targets": target_rows,
        "likely_comps": likely_comps,
        "status": "ready",
    }


def flip_result(result: str) -> str:
    if result == "Win":
        return "Loss"
    if result == "Loss":
        return "Win"
    return result


def to_enemy_perspective_scrims(scrims: list[dict]) -> list[dict]:
    transformed_scrims = []
    for scrim in scrims:
        transformed_maps = []
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            enemy_team_slot = opposite_team_slot(our_team_slot)

            transformed_map = dict(map_entry)
            transformed_map["our_team_slot"] = enemy_team_slot
            transformed_map["result"] = flip_result(map_entry.get("result", ""))
            transformed_map["score"] = flip_score_text(map_entry.get("score", ""))
            transformed_sections = []
            for section in map_entry.get("comp", []):
                transformed_section = dict(section)
                transformed_section["score"] = flip_score_text(section.get("score", ""))
                transformed_sections.append(transformed_section)
            transformed_map["comp"] = transformed_sections
            transformed_maps.append(transformed_map)

        transformed_scrim = dict(scrim)
        transformed_scrim["maps"] = transformed_maps
        transformed_scrims.append(transformed_scrim)

    return transformed_scrims


def build_map_mode_breakdown(scrims: list[dict]) -> tuple[list[dict], list[dict], dict | None, dict | None]:
    map_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    mode_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    map_type_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    opponent_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    recent_map_visual_rows: list[dict] = []
    map_timeline_targets: dict[str, int] = {}
    side_score_records = defaultdict(
        lambda: {
            "Attack": {"sum": 0.0, "count": 0},
            "Defense": {"sum": 0.0, "count": 0},
        }
    )

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            map_name = (map_entry.get("map_name", "") or "").strip()
            if not map_name:
                continue

            if map_name not in map_timeline_targets and scrim.get("id") is not None:
                map_timeline_targets[map_name] = scrim.get("id")

            mode_name = MAP_MODES.get(map_name, "Other")
            result = map_entry.get("result")

            map_records[map_name]["maps"] += 1
            mode_records[mode_name]["maps"] += 1

            if result == "Win":
                map_records[map_name]["wins"] += 1
                mode_records[mode_name]["wins"] += 1
            elif result == "Loss":
                map_records[map_name]["losses"] += 1
                mode_records[mode_name]["losses"] += 1

            for section in map_entry.get("comp", []):
                section_side = (section.get("side") or "").strip()
                if section_side not in ("Attack", "Defense"):
                    continue
                numeric_score = score_for_perspective(section.get("score", ""), perspective="left")
                if numeric_score is None:
                    continue
                side_score_records[map_name][section_side]["sum"] += numeric_score
                side_score_records[map_name][section_side]["count"] += 1

    team_map_cards = []
    for map_name, stats in map_records.items():
        maps_played = stats["maps"]
        win_rate = round((stats["wins"] / maps_played) * 100, 1) if maps_played else 0
        team_map_cards.append(
            {
                "map_name": map_name,
                "mode": MAP_MODES.get(map_name, "Other"),
                "maps": maps_played,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": win_rate,
                "image": MAP_IMAGES.get(map_name, ""),
                "attack_score_avg": (
                    round(
                        side_score_records[map_name]["Attack"]["sum"]
                        / side_score_records[map_name]["Attack"]["count"],
                        2,
                    )
                    if side_score_records[map_name]["Attack"]["count"]
                    else None
                ),
                "defense_score_avg": (
                    round(
                        side_score_records[map_name]["Defense"]["sum"]
                        / side_score_records[map_name]["Defense"]["count"],
                        2,
                    )
                    if side_score_records[map_name]["Defense"]["count"]
                    else None
                ),
            }
        )
    team_map_cards.sort(key=lambda r: (r["win_rate"], r["maps"]), reverse=True)

    team_map_mode_rows = []
    for mode_name, stats in mode_records.items():
        maps_played = stats["maps"]
        win_rate = round((stats["wins"] / maps_played) * 100, 1) if maps_played else 0
        mode_maps = [card for card in team_map_cards if card["mode"] == mode_name]
        best_map = max(mode_maps, key=lambda m: (m["win_rate"], m["maps"]), default=None)
        worst_map = min(mode_maps, key=lambda m: (m["win_rate"], -m["maps"]), default=None)
        team_map_mode_rows.append(
            {
                "mode": mode_name,
                "maps": maps_played,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": win_rate,
                "best_map": best_map,
                "worst_map": worst_map,
            }
        )
    team_map_mode_rows.sort(key=lambda r: (r["win_rate"], r["maps"]), reverse=True)

    best_mode = team_map_mode_rows[0] if team_map_mode_rows else None
    worst_mode = team_map_mode_rows[-1] if team_map_mode_rows else None
    return team_map_cards, team_map_mode_rows, best_mode, worst_mode


def _hero_match_key(hero_name: str) -> str:
    resolved = _resolve_hero_transform_key(hero_name)
    return _compact_text(resolved or hero_name)


def _hero_role(hero_name: str) -> str:
    key = _hero_match_key(hero_name)
    if not key:
        return ""
    for role_name, heroes in HERO_ROLES.items():
        for hero in heroes:
            if _compact_text(hero) == key:
                return role_name
    return ""


def _canonical_section_hero_instances(section: dict, team_slot: str) -> list[str]:
    lineup = section.get(team_slot, []) if isinstance(section, dict) else []
    if not isinstance(lineup, list):
        return []

    instances: list[str] = []
    for slot in lineup:
        if not isinstance(slot, dict):
            continue
        hero_name = _canonical_draft_hero(slot.get("hero", ""))
        if hero_name:
            instances.append(hero_name)
    return instances


def _canonical_map_hero_instances(map_entry: dict, team_slot: str) -> list[str]:
    instances: list[str] = []
    for section in map_entry.get("comp", []):
        instances.extend(_canonical_section_hero_instances(section, team_slot))
    return instances


def build_team_hero_insights(team_scrims: list[dict], hero_name: str) -> dict:
    target_name = (hero_name or "").strip()
    target_key = _hero_match_key(target_name)
    display_name = _resolve_hero_transform_key(target_name) or target_name
    target_role = _hero_role(display_name)

    ally_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    duo_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    comp_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    map_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})

    total_maps = 0
    total_wins = 0
    total_losses = 0
    total_instances = 0
    timeline_points = []

    ban_tracked_maps = 0
    banned_maps = 0
    open_maps = 0
    banned_wins = 0
    banned_losses = 0
    open_wins = 0
    open_losses = 0
    ban_pivot_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})

    sorted_scrims = sorted(team_scrims, key=lambda s: (s.get("scrim_date", ""), s.get("id", 0)))

    for scrim in sorted_scrims:
        scrim_maps = 0
        scrim_wins = 0
        scrim_losses = 0

        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            result = get_map_outcome_for_slot(map_entry, our_team_slot)

            enemy_team_slot = "team2" if our_team_slot == "team1" else "team1"
            draft = map_entry.get("draft") if isinstance(map_entry.get("draft"), dict) else {}
            enemy_draft = draft.get(enemy_team_slot) if isinstance(draft.get(enemy_team_slot), dict) else {}
            enemy_ban_keys = ["ban1", "ban2", "ban3", "ban4"]
            enemy_bans = [
                _canonical_draft_hero(enemy_draft.get(k, ""))
                for k in enemy_ban_keys
                if _canonical_draft_hero(enemy_draft.get(k, ""))
            ]
            has_enemy_ban_data = bool(enemy_bans)
            target_banned = any(_hero_match_key(b) == target_key for b in enemy_bans)
            if has_enemy_ban_data:
                ban_tracked_maps += 1
                if target_banned:
                    banned_maps += 1
                    if result == "Win":
                        banned_wins += 1
                    elif result == "Loss":
                        banned_losses += 1
                else:
                    open_maps += 1
                    if result == "Win":
                        open_wins += 1
                    elif result == "Loss":
                        open_losses += 1

            map_has_hero = False
            map_instances = 0
            for section in map_entry.get("comp", []):
                our_heroes = _canonical_section_hero_instances(section, our_team_slot)
                if not our_heroes:
                    continue

                target_instances = sum(1 for hero in our_heroes if _hero_match_key(hero) == target_key)
                if not target_instances:
                    continue

                map_has_hero = True
                map_instances += target_instances

                teammates = [hero for hero in our_heroes if _hero_match_key(hero) != target_key]
                for teammate in teammates:
                    ally_stats[teammate]["count"] += 1
                    if result == "Win":
                        ally_stats[teammate]["wins"] += 1
                    elif result == "Loss":
                        ally_stats[teammate]["losses"] += 1

                if target_role:
                    same_role_partners = [
                        hero
                        for hero in our_heroes
                        if _hero_match_key(hero) != target_key and _hero_role(hero) == target_role
                    ]
                    for duo_partner in same_role_partners:
                        duo_stats[duo_partner]["count"] += 1
                        if result == "Win":
                            duo_stats[duo_partner]["wins"] += 1
                        elif result == "Loss":
                            duo_stats[duo_partner]["losses"] += 1

                comp_signature = tuple(sorted(our_heroes))
                if comp_signature:
                    comp_stats[comp_signature]["count"] += 1
                    if result == "Win":
                        comp_stats[comp_signature]["wins"] += 1
                    elif result == "Loss":
                        comp_stats[comp_signature]["losses"] += 1

                if target_banned:
                    for pivot_hero in teammates:
                        ban_pivot_stats[pivot_hero]["count"] += 1
                        if result == "Win":
                            ban_pivot_stats[pivot_hero]["wins"] += 1
                        elif result == "Loss":
                            ban_pivot_stats[pivot_hero]["losses"] += 1

            if not map_has_hero:
                continue

            map_name = (map_entry.get("map_name") or "").strip()
            if map_name:
                map_stats[map_name]["maps"] += 1

            total_maps += 1
            total_instances += map_instances
            scrim_maps += 1
            if result == "Win":
                total_wins += 1
                scrim_wins += 1
                if map_name:
                    map_stats[map_name]["wins"] += 1
            elif result == "Loss":
                total_losses += 1
                scrim_losses += 1
                if map_name:
                    map_stats[map_name]["losses"] += 1

        if scrim_maps:
            label = f"{scrim.get('scrim_date', '')} vs {scrim.get('enemy_team') or scrim.get('opponent') or 'Unknown'}"
            timeline_points.append(
                {
                    "label": label,
                    "maps": scrim_maps,
                    "wins": scrim_wins,
                    "losses": scrim_losses,
                    "scrim_win_rate": round((scrim_wins / scrim_maps) * 100, 1) if scrim_maps else 0,
                    "cumulative_win_rate": round((total_wins / total_maps) * 100, 1) if total_maps else 0,
                }
            )

    ally_rows = []
    for ally_name, stats in ally_stats.items():
        count = stats["count"]
        ally_rows.append(
            {
                "hero": ally_name,
                "count": count,
                "win_rate": round((stats["wins"] / count) * 100, 1) if count else 0,
            }
        )
    ally_rows.sort(key=lambda r: (r["count"], r["win_rate"]), reverse=True)

    duo_rows = []
    for duo_name, stats in duo_stats.items():
        count = stats["count"]
        duo_rows.append(
            {
                "hero": duo_name,
                "count": count,
                "win_rate": round((stats["wins"] / count) * 100, 1) if count else 0,
            }
        )
    duo_rows.sort(key=lambda r: (r["count"], r["win_rate"]), reverse=True)

    comp_rows = []
    for comp_signature, stats in comp_stats.items():
        count = stats["count"]
        comp_rows.append(
            {
                "heroes": list(comp_signature),
                "count": count,
                "win_rate": round((stats["wins"] / count) * 100, 1) if count else 0,
            }
        )
    comp_rows.sort(key=lambda r: (r["count"], r["win_rate"]), reverse=True)

    map_rows = []
    for map_name, stats in map_stats.items():
        maps_played = stats["maps"]
        map_rows.append(
            {
                "map_name": map_name,
                "mode": MAP_MODES.get(map_name, "Unknown"),
                "maps": maps_played,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round((stats["wins"] / maps_played) * 100, 1) if maps_played else 0,
                "image": MAP_IMAGES.get(map_name, ""),
            }
        )
    map_rows.sort(key=lambda r: (r["maps"], r["win_rate"]), reverse=True)

    open_decisions = open_wins + open_losses
    banned_decisions = banned_wins + banned_losses
    open_wr = round((open_wins / open_decisions) * 100, 1) if open_decisions else None
    banned_wr = round((banned_wins / banned_decisions) * 100, 1) if banned_decisions else None
    open_vs_banned_delta = round(open_wr - banned_wr, 1) if (open_wr is not None and banned_wr is not None) else None

    ban_pivot_rows = []
    for pivot_hero, stats in ban_pivot_stats.items():
        count = stats["count"]
        if not count:
            continue
        decisions = stats["wins"] + stats["losses"]
        ban_pivot_rows.append(
            {
                "hero": pivot_hero,
                "count": count,
                "win_rate": round((stats["wins"] / decisions) * 100, 1) if decisions else None,
            }
        )
    ban_pivot_rows.sort(key=lambda row: (row["count"], row["win_rate"] or 0), reverse=True)

    return {
        "hero": display_name,
        "target_role": target_role,
        "hero_image_url": _hero_image_url(display_name),
        "summary": {
            "maps_played": total_maps,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate": round((total_wins / total_maps) * 100, 1) if total_maps else 0,
            "hero_instances": total_instances,
            "unique_allies": len(ally_rows),
        },
        "ally_rows": ally_rows,
        "duo_rows": duo_rows,
        "comp_rows": comp_rows,
        "map_rows": map_rows,
        "timeline_points": timeline_points,
        "ban_impact": {
            "tracked_maps": ban_tracked_maps,
            "banned_maps": banned_maps,
            "open_maps": open_maps,
            "banned_rate": round((banned_maps / ban_tracked_maps) * 100, 1) if ban_tracked_maps else 0,
            "win_rate_when_open": open_wr,
            "win_rate_when_banned": banned_wr,
            "open_vs_banned_delta": open_vs_banned_delta,
            "open_decisions": open_decisions,
            "banned_decisions": banned_decisions,
            "top_pivots": ban_pivot_rows[:5],
        },
    }


def build_hero_usage_timeline(team_scrims: list[dict], top_heroes: list[str]) -> dict:
    labels = []
    series_map = {hero: [] for hero in top_heroes}

    sorted_scrims = sorted(team_scrims, key=lambda s: (s.get("scrim_date", ""), s.get("id", 0)))

    for scrim in sorted_scrims:
        maps = scrim.get("maps", [])
        map_count = len(maps)
        if not map_count:
            continue

        hero_instance_counts = {hero: 0 for hero in top_heroes}
        total_instances = 0
        for map_entry in maps:
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            map_instances = _canonical_map_hero_instances(map_entry, our_team_slot)
            total_instances += len(map_instances)
            for hero_name in map_instances:
                if hero_name in hero_instance_counts:
                    hero_instance_counts[hero_name] += 1

        labels.append(f"{scrim.get('scrim_date', '')} vs {scrim.get('enemy_team') or scrim.get('opponent') or 'Unknown'}")
        for hero in top_heroes:
            usage_rate = round((hero_instance_counts[hero] / total_instances) * 100, 1) if total_instances else 0
            series_map[hero].append(usage_rate)

    series = [
        {
            "hero": hero,
            "values": series_map[hero],
        }
        for hero in top_heroes
    ]
    return {
        "labels": labels,
        "series": series,
    }


def build_team_hero_profile(team_scrims: list[dict], players: list[dict]) -> dict:
    role_order = {"Vanguard": 0, "Duelist": 1, "Strategist": 2, "Flex": 3}
    hero_map_stats = defaultdict(lambda: {"appearances": 0, "wins": 0, "losses": 0, "players": set()})
    player_instance_totals = defaultdict(int)
    player_hero_counts = defaultdict(lambda: defaultdict(int))
    player_hero_wins = defaultdict(lambda: defaultdict(int))
    tracked_maps = 0

    player_lookup = {}
    for player in players:
        player_name = (player.get("name", "") or "").strip()
        if not player_name:
            continue
        player_lookup[player_name.lower()] = {
            "id": player.get("id"),
            "name": player_name,
            "role": (player.get("role", "") or "").strip(),
        }

    for scrim in team_scrims:
        for map_entry in scrim.get("maps", []):
            tracked_maps += 1
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            result = get_map_outcome_for_slot(map_entry, our_team_slot)
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue

                lineup = section.get(our_team_slot, [])
                if not isinstance(lineup, list):
                    continue

                for slot in lineup:
                    if not isinstance(slot, dict):
                        continue

                    hero_name = _canonical_draft_hero(slot.get("hero", ""))
                    player_name = (slot.get("player", "") or "").strip()
                    if not hero_name:
                        continue

                    hero_map_stats[hero_name]["appearances"] += 1
                    if result == "Win":
                        hero_map_stats[hero_name]["wins"] += 1
                    elif result == "Loss":
                        hero_map_stats[hero_name]["losses"] += 1

                    if player_name:
                        player_key = player_name.lower()
                        hero_map_stats[hero_name]["players"].add(player_name)
                        player_instance_totals[player_key] += 1
                        player_hero_counts[player_key][hero_name] += 1
                        if result == "Win":
                            player_hero_wins[player_key][hero_name] += 1
                        if player_key not in player_lookup:
                            player_lookup[player_key] = {
                                "id": None,
                                "name": player_name,
                                "role": "",
                            }

    hero_rows = []
    total_hero_instances = 0
    for hero_name, stats in hero_map_stats.items():
        appearances = stats["appearances"]
        total_hero_instances += appearances
        hero_rows.append(
            {
                "hero": hero_name,
                "appearances": appearances,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round((stats["wins"] / appearances) * 100, 1) if appearances else 0,
                "usage_rate": 0,
                "player_count": len([name for name in stats["players"] if name]),
            }
        )
    for row in hero_rows:
        appearances = row["appearances"]
        row["usage_rate"] = round((appearances / total_hero_instances) * 100, 1) if total_hero_instances else 0
    hero_rows.sort(key=lambda row: (row["appearances"], row["win_rate"], row["hero"].lower()), reverse=True)

    effective_pool_threshold = max(2, math.ceil(tracked_maps * 0.1)) if tracked_maps else 0
    effective_pool = sum(1 for row in hero_rows if row["appearances"] >= effective_pool_threshold) if effective_pool_threshold else 0

    if len(hero_rows) > 1 and total_hero_instances:
        entropy = 0.0
        for row in hero_rows:
            share = row["appearances"] / total_hero_instances
            if share > 0:
                entropy -= share * math.log(share)
        diversity_score = round((entropy / math.log(len(hero_rows))) * 100, 1)
    else:
        diversity_score = 0.0

    specialists = []
    top_hero_names = [row["hero"] for row in hero_rows[:15]]
    heatmap_rows = []
    ordered_players = sorted(
        players,
        key=lambda row: (
            role_order.get((row.get("role", "") or "").strip(), 99),
            (row.get("name", "") or "").strip().lower(),
        ),
    )

    for player in ordered_players:
        player_name = (player.get("name", "") or "").strip()
        if not player_name:
            continue

        player_key = player_name.lower()
        total_appearances = player_instance_totals.get(player_key, 0)
        hero_counts = player_hero_counts.get(player_key, {})
        sorted_hero_rows = [
            {
                "hero": hero_name,
                "appearances": count,
                "rate": round((count / total_appearances) * 100, 1) if total_appearances else 0,
                "win_rate": round((player_hero_wins[player_key][hero_name] / count) * 100, 1) if count else 0,
            }
            for hero_name, count in sorted(hero_counts.items(), key=lambda item: (item[1], item[0].lower()), reverse=True)
        ]

        top_row = sorted_hero_rows[0] if sorted_hero_rows else None
        top_two_rate = round((sum(row["appearances"] for row in sorted_hero_rows[:2]) / total_appearances) * 100, 1) if total_appearances else 0
        if top_row and total_appearances >= 3 and (top_row["rate"] >= 45 or top_two_rate >= 70):
            specialists.append(
                {
                    "player_id": player.get("id"),
                    "player_name": player_name,
                    "role": (player.get("role", "") or "").strip(),
                    "appearances": total_appearances,
                    "focus_hero": top_row["hero"],
                    "focus_appearances": top_row["appearances"],
                    "focus_rate": top_row["rate"],
                    "top_two_rate": top_two_rate,
                    "unique_heroes": len(sorted_hero_rows),
                    "hero_rows": sorted_hero_rows[:3],
                }
            )

        heatmap_cells = []
        active_heroes = 0
        for hero_name in top_hero_names:
            count = hero_counts.get(hero_name, 0)
            rate = round((count / total_appearances) * 100, 1) if total_appearances else 0
            win_rate = round((player_hero_wins[player_key][hero_name] / count) * 100, 1) if count else 0
            intensity = 0
            if count and total_appearances:
                intensity = max(16, min(100, int(round(rate))))
                active_heroes += 1
            heatmap_cells.append(
                {
                    "count": count,
                    "rate": rate,
                    "win_rate": win_rate,
                    "intensity": intensity,
                }
            )

        heatmap_rows.append(
            {
                "player_id": player.get("id"),
                "player_name": player_name,
                "role": (player.get("role", "") or "").strip(),
                "appearances": total_appearances,
                "active_heroes": active_heroes,
                "cells": heatmap_cells,
            }
        )

    specialists.sort(
        key=lambda row: (
            row["focus_appearances"],
            row["focus_rate"],
            row["appearances"],
            row["player_name"].lower(),
        ),
        reverse=True,
    )

    return {
        "summary": {
            "tracked_maps": tracked_maps,
            "total_instances": total_hero_instances,
            "total_heroes": len(hero_rows),
            "effective_pool": effective_pool,
            "effective_pool_threshold": effective_pool_threshold,
            "diversity_score": diversity_score,
            "specialist_count": len(specialists),
        },
        "hero_rows": hero_rows,
        "top_heroes": hero_rows[:15],
        "specialists": specialists,
        "heatmap_columns": top_hero_names,
        "heatmap_rows": heatmap_rows,
    }


@app.route("/")
def dashboard():
    db = get_db()
    total_scrims = len(SCRIMS)
    total_tournaments = len(TOURNAMENT_MATCHES)
    total_maps = sum(len(scrim["maps"]) for scrim in SCRIMS) + sum(len(match["maps"]) for match in TOURNAMENT_MATCHES)
    total_events = (
        sum(len(map_entry["events"]) for scrim in SCRIMS for map_entry in scrim["maps"])
        + sum(len(map_entry.get("events", [])) for match in TOURNAMENT_MATCHES for map_entry in match["maps"])
    )
    total_teams = db.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    total_players = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]

    personal_team_rows = db.execute(
        "SELECT id, name FROM teams WHERE is_personal = 1"
    ).fetchall()

    pick_counter: Counter = Counter()
    ban_counter: Counter = Counter()
    opponent_records = defaultdict(lambda: {"team_id": None, "name": "", "scrims": 0, "maps": 0})
    personal_quick_teams = []
    seen_scrims: set = set()

    for team_row in personal_team_rows:
        team_scrims = get_scrims_for_team(team_row["id"], team_row["name"])
        personal_quick_teams.append(
            {
                "id": team_row["id"],
                "name": team_row["name"],
                "scrims": len(team_scrims),
                "maps": sum(len(scrim.get("maps", [])) for scrim in team_scrims),
            }
        )
        for scrim in team_scrims:
            scrim_id = scrim.get("id")
            if scrim_id in seen_scrims:
                continue
            seen_scrims.add(scrim_id)

            opponent_name = (
                (scrim.get("enemy_team", "") or "").strip()
                or (scrim.get("opponent", "") or "").strip()
                or "Opponent"
            )
            opponent_team_id = scrim.get("enemy_team_id")
            opponent_key = (
                f"id:{int(opponent_team_id)}"
                if isinstance(opponent_team_id, int) and opponent_team_id > 0
                else f"name:{opponent_name.lower()}"
            )
            opponent_records[opponent_key]["team_id"] = opponent_team_id if isinstance(opponent_team_id, int) and opponent_team_id > 0 else None
            opponent_records[opponent_key]["name"] = opponent_name
            opponent_records[opponent_key]["scrims"] += 1
            opponent_records[opponent_key]["maps"] += len(scrim.get("maps", []))

            for map_entry in scrim.get("maps", []):
                if not isinstance(map_entry, dict):
                    continue
                our_slot = map_entry.get("our_team_slot", "team1")
                for section in map_entry.get("comp", []):
                    if not isinstance(section, dict):
                        continue
                    for slot in section.get(our_slot, []):
                        if not isinstance(slot, dict):
                            continue
                        hero = canonicalize_hero_name(slot.get("hero", ""))
                        if hero:
                            pick_counter[hero] += 1
                draft = map_entry.get("draft", {})
                if isinstance(draft, dict):
                    our_draft = draft.get(our_slot, {})
                    if isinstance(our_draft, dict):
                        for ban_key in ("ban1", "ban2", "ban3", "ban4"):
                            hero = canonicalize_hero_name(our_draft.get(ban_key, ""))
                            if hero:
                                ban_counter[hero] += 1

    top_picks = [{"hero": h, "count": c} for h, c in pick_counter.most_common(5)]
    top_bans = [{"hero": h, "count": c} for h, c in ban_counter.most_common(5)]
    personal_quick_teams.sort(
        key=lambda row: (row["maps"], row["scrims"], row["name"].lower()),
        reverse=True,
    )
    quick_opponents = sorted(
        opponent_records.values(),
        key=lambda row: (row["maps"], row["scrims"], row["name"]),
        reverse=True,
    )[:8]

    all_team_rows = db.execute(
        "SELECT id, name, logo_path, is_personal FROM teams ORDER BY name COLLATE NOCASE"
    ).fetchall()
    all_teams_for_quick_access = [
        {
            "id": row["id"],
            "name": row["name"],
            "logo_path": row["logo_path"],
            "is_personal": bool(row["is_personal"]),
        }
        for row in all_team_rows
    ]

    return render_template(
        "dashboard.html",
        total_scrims=total_scrims,
        total_tournaments=total_tournaments,
        total_maps=total_maps,
        total_events=total_events,
        total_teams=total_teams,
        total_players=total_players,
        recent_scrims=list(reversed(SCRIMS[-5:])),
        recent_tournaments=list(reversed(TOURNAMENT_MATCHES[-5:])),
        top_picks=top_picks,
        top_bans=top_bans,
        personal_quick_teams=personal_quick_teams,
        quick_opponents=quick_opponents,
        all_teams_for_quick_access=all_teams_for_quick_access,
    )


@app.route("/api/teams/<int:team_id>/set-personal", methods=["POST"])
def api_set_personal_team(team_id: int):
    if not _is_edit_session():
        return jsonify({"error": "Unauthorized"}), 403
    
    db = get_db()
    team = db.execute("SELECT id FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team:
        return jsonify({"error": "Team not found"}), 404
    
    db.execute("UPDATE teams SET is_personal = 0")
    db.execute("UPDATE teams SET is_personal = 1 WHERE id = ?", (team_id,))
    db.commit()
    
    return jsonify({"success": True})


@app.route("/teams")
def teams():
    db = get_db()
    migrate_enemy_teams_to_team_database(db)
    team_rows = db.execute(
        """
        SELECT t.id, t.name, t.notes, t.quality_tag, t.logo_path, t.is_personal, COUNT(p.id) AS player_count
        FROM teams t
        LEFT JOIN players p ON p.team_id = t.id
        GROUP BY t.id
        ORDER BY t.name COLLATE NOCASE
        """
    ).fetchall()

    season_options = get_scrim_season_options(SCRIMS)
    default_season = get_current_season_from_recent_scrim(SCRIMS)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in SCRIMS)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )

    teams_with_scrim_stats = []
    for row in team_rows:
        all_team_scrims = get_scrims_for_team(row["id"], row["name"])
        team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
        team_maps = sum(len(scrim.get("maps", [])) for scrim in team_scrims)
        team_wins = sum(
            1
            for scrim in team_scrims
            for map_entry in scrim.get("maps", [])
            if get_map_outcome_for_slot(map_entry, map_entry.get("our_team_slot", "team1")) == "Win"
        )
        team_win_rate = round((team_wins / team_maps) * 100, 1) if team_maps else 0

        # Calculate hero pool (top 5 heroes)
        pick_counter: Counter = Counter()
        for scrim in team_scrims:
            for map_entry in scrim.get("maps", []):
                if not isinstance(map_entry, dict):
                    continue
                our_slot = map_entry.get("our_team_slot", "team1")
                for section in map_entry.get("comp", []):
                    if not isinstance(section, dict):
                        continue
                    for slot in section.get(our_slot, []):
                        if not isinstance(slot, dict):
                            continue
                        hero = canonicalize_hero_name(slot.get("hero", ""))
                        if hero:
                            pick_counter[hero] += 1

        hero_pool = [{"hero": h, "count": c} for h, c in pick_counter.most_common(5)]

        teams_with_scrim_stats.append(
            {
                "id": row["id"],
                "name": row["name"],
                "notes": row["notes"],
                "quality_tag": row["quality_tag"],
                "logo_path": row["logo_path"],
                "is_personal": bool(row["is_personal"]),
                "player_count": row["player_count"],
                "scrim_count": len(team_scrims),
                "map_count": team_maps,
                "map_win_rate": team_win_rate,
                "hero_pool": hero_pool,
            }
        )

    personal_teams = [team for team in teams_with_scrim_stats if team["is_personal"]]

    return render_template(
        "teams.html",
        teams=teams_with_scrim_stats,
        personal_teams=personal_teams,
        season_options=season_options,
        selected_season=selected_season,
        has_unseasoned_scrims=has_unseasoned_scrims,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
    )


@app.route("/teams/compare")
def teams_compare():
    db = get_db()
    team_rows = db.execute(
        "SELECT id, name, notes, logo_path, is_personal FROM teams ORDER BY name COLLATE NOCASE"
    ).fetchall()
    team_options = [dict(row) for row in team_rows]
    team_lookup = {str(row["id"]): dict(row) for row in team_rows}

    selected_team_a_id = (request.args.get("team_a") or "").strip()
    selected_team_b_id = (request.args.get("team_b") or "").strip()
    selected_mode = (request.args.get("mode") or "scrims").strip().lower()
    if selected_mode not in {"scrims", "tournaments"}:
        selected_mode = "scrims"

    def _ban_rate_map(analytics: dict) -> dict[str, float]:
        return {row.get("hero", ""): float(row.get("ban_rate", 0)) for row in analytics.get("ban_rows", []) if row.get("hero")}

    def _protect_rate_map(analytics: dict) -> dict[str, float]:
        return {row.get("hero", ""): float(row.get("protect_rate", 0)) for row in analytics.get("protect_rows", []) if row.get("hero")}

    def _open_rows_map(analytics: dict) -> dict[str, dict]:
        return {row.get("hero", ""): row for row in analytics.get("hero_open_rows", []) if row.get("hero")}

    def load_team_payload(team_row: dict | None) -> dict | None:
        if team_row is None:
            return None

        scrim_pool = get_scrims_for_team(team_row["id"], team_row["name"])
        tournament_pool = build_team_tournament_scrims(team_row)
        source_pool = tournament_pool if selected_mode == "tournaments" else scrim_pool
        latest_season = get_current_season_from_recent_scrim(source_pool)
        team_scrims = filter_scrims_by_season(source_pool, latest_season)
        analytics = build_scrim_analytics(team_scrims)
        return {
            "team": team_row,
            "analytics": analytics,
            "top_heroes": analytics.get("hero_rows", [])[:8],
            "top_maps": analytics.get("map_rows", [])[:8],
            "all_heroes": analytics.get("hero_rows", []),
            "all_maps": analytics.get("map_rows", []),
            "latest_season": latest_season,
            "ban_rate_map": _ban_rate_map(analytics),
            "protect_rate_map": _protect_rate_map(analytics),
            "open_rows_map": _open_rows_map(analytics),
            "flow_rows": analytics.get("ban_next_rows", [])[:6],
        }

    team_a = load_team_payload(team_lookup.get(selected_team_a_id))
    team_b = load_team_payload(team_lookup.get(selected_team_b_id))

    ban_matchup_rows = []
    shared_heroes_rows = []
    shared_maps_rows = []
    if team_a and team_b:
        # Shared hero WR comparison
        a_hero_map = {r["hero"]: r for r in team_a["all_heroes"]}
        b_hero_map = {r["hero"]: r for r in team_b["all_heroes"]}
        for hero, a_row in a_hero_map.items():
            if hero in b_hero_map:
                b_row = b_hero_map[hero]
                shared_heroes_rows.append({
                    "hero": hero,
                    "a_maps": a_row["maps"],
                    "a_wr": a_row["win_rate"],
                    "b_maps": b_row["maps"],
                    "b_wr": b_row["win_rate"],
                    "wr_diff": round(a_row["win_rate"] - b_row["win_rate"], 1),
                })
        shared_heroes_rows.sort(key=lambda r: r["a_maps"] + r["b_maps"], reverse=True)

        # Shared map WR comparison
        a_map_map = {r["map_name"]: r for r in team_a["all_maps"]}
        b_map_map = {r["map_name"]: r for r in team_b["all_maps"]}
        for map_name, a_row in a_map_map.items():
            if map_name in b_map_map:
                b_row = b_map_map[map_name]
                shared_maps_rows.append({
                    "map_name": map_name,
                    "a_maps": a_row["maps"],
                    "a_wr": a_row["win_rate"],
                    "b_maps": b_row["maps"],
                    "b_wr": b_row["win_rate"],
                    "wr_diff": round(a_row["win_rate"] - b_row["win_rate"], 1),
                })
        shared_maps_rows.sort(key=lambda r: r["a_maps"] + r["b_maps"], reverse=True)

        hero_candidates = []
        hero_candidates.extend([row.get("hero", "") for row in team_a["analytics"].get("ban_rows", [])[:8]])
        hero_candidates.extend([row.get("hero", "") for row in team_b["analytics"].get("ban_rows", [])[:8]])

        seen = set()
        ordered_heroes = []
        for hero in hero_candidates:
            if not hero or hero in seen:
                continue
            seen.add(hero)
            ordered_heroes.append(hero)

        for hero in ordered_heroes:
            a_ban_rate = float(team_a["ban_rate_map"].get(hero, 0))
            b_ban_rate = float(team_b["ban_rate_map"].get(hero, 0))
            a_protect_rate = float(team_a["protect_rate_map"].get(hero, 0))
            b_protect_rate = float(team_b["protect_rate_map"].get(hero, 0))
            a_open_row = team_a["open_rows_map"].get(hero, {})
            b_open_row = team_b["open_rows_map"].get(hero, {})
            ban_matchup_rows.append(
                {
                    "hero": hero,
                    "a_ban_rate": round(a_ban_rate, 1),
                    "b_ban_rate": round(b_ban_rate, 1),
                    "ban_rate_diff": round(a_ban_rate - b_ban_rate, 1),
                    "a_protect_rate": round(a_protect_rate, 1),
                    "b_protect_rate": round(b_protect_rate, 1),
                    "protect_rate_diff": round(a_protect_rate - b_protect_rate, 1),
                    "a_open_wr": a_open_row.get("win_rate_when_open"),
                    "b_open_wr": b_open_row.get("win_rate_when_open"),
                    "a_banned_wr": a_open_row.get("win_rate_when_banned"),
                    "b_banned_wr": b_open_row.get("win_rate_when_banned"),
                }
            )

    return render_template(
        "teams_compare.html",
        team_options=team_options,
        selected_team_a_id=selected_team_a_id,
        selected_team_b_id=selected_team_b_id,
        selected_mode=selected_mode,
        team_a=team_a,
        team_b=team_b,
        ban_matchup_rows=ban_matchup_rows,
        shared_heroes_rows=shared_heroes_rows,
        shared_maps_rows=shared_maps_rows,
        map_images=MAP_IMAGES,
    )


@app.route("/teams/create", methods=["POST"])
def create_team():
    db = get_db()
    raw_name = request.form.get("name", "")
    name = normalize_player_name(raw_name)
    notes = request.form.get("notes", "").strip()
    quality_tag_raw = " ".join(request.form.get("quality_tag", "").strip().split())[:32]
    quality_tag = quality_tag_raw if quality_tag_raw in TEAM_QUALITY_TAG_OPTIONS else ""
    logo_path = save_team_logo(request.files.get("logo"), name)
    is_personal = 1 if request.form.get("is_personal", "").strip() == "1" else 0

    if not name:
        flash("Team name is required.", "error")
        return redirect(url_for("teams"))

    try:
        db.execute(
            "INSERT INTO teams (name, notes, quality_tag, logo_path, is_personal) VALUES (?, ?, ?, ?, ?)",
            (name, notes, quality_tag, logo_path, is_personal),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash("A team with that name already exists.", "error")
        return redirect(url_for("teams"))

    flash("Team created.", "success")
    return redirect(url_for("teams"))


@app.route("/teams/<int:team_id>/edit", methods=["POST"])
def edit_team(team_id: int):
    db = get_db()
    current = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if current is None:
        abort(404)

    raw_name = request.form.get("name", "")
    name = normalize_player_name(raw_name)
    notes = request.form.get("notes", "").strip()
    quality_tag_raw = " ".join(request.form.get("quality_tag", "").strip().split())[:32]
    quality_tag = quality_tag_raw if quality_tag_raw in TEAM_QUALITY_TAG_OPTIONS else ""
    remove_logo = request.form.get("remove_logo", "").strip() == "1"
    new_logo_path = save_team_logo(request.files.get("logo"), name)
    raw_personal = request.form.get("is_personal")
    if raw_personal is None:
        is_personal = int(current["is_personal"] or 0)
    else:
        is_personal = 1 if (raw_personal or "").strip() == "1" else 0
    if not name:
        flash("Team name is required.", "error")
        return redirect(url_for("team_detail", team_id=team_id))

    try:
        current_logo_path = current["logo_path"]
        logo_path = current_logo_path
        if new_logo_path:
            logo_path = new_logo_path
            if current_logo_path and current_logo_path != new_logo_path:
                delete_team_logo_file(current_logo_path)
        elif remove_logo and current_logo_path:
            logo_path = ""
            delete_team_logo_file(current_logo_path)
        db.execute(
            "UPDATE teams SET name = ?, notes = ?, quality_tag = ?, logo_path = ?, is_personal = ? WHERE id = ?",
            (name, notes, quality_tag, logo_path, is_personal, team_id),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash("A team with that name already exists.", "error")
        return redirect(url_for("team_detail", team_id=team_id))

    flash("Team updated.", "success")
    return redirect(url_for("team_detail", team_id=team_id))


@app.route("/teams/<int:team_id>/quick-access", methods=["POST"])
def toggle_team_quick_access(team_id: int):
    db = get_db()
    team = db.execute("SELECT id, is_personal FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    action = (request.form.get("action") or "toggle").strip().lower()
    current_value = 1 if team["is_personal"] else 0
    if action == "add":
        next_value = 1
    elif action == "remove":
        next_value = 0
    else:
        next_value = 0 if current_value else 1

    db.execute("UPDATE teams SET is_personal = ? WHERE id = ?", (next_value, team_id))
    db.commit()

    flash("Quick access updated.", "success")
    return redirect(url_for("teams", season=request.form.get("season", "all")))


def build_pivot_wr(team_scrims: list[dict]) -> dict:
    """Track hero-switch (pivot) win rates on attack rounds for Escort/Hybrid maps.

    A pivot is detected when a player:
      1. Played hero X on attack and the attack was LOST.
      2. In their next recorded attack appearance, played a DIFFERENT hero Y.

    Returns per-player and per-hero-pair pivot stats.
    """
    from collections import defaultdict

    # Accumulate per-player ordered list of (hero, atk_won) for attack rounds only.
    player_atk_history: dict[str, list[tuple[str, bool]]] = defaultdict(list)

    for scrim in team_scrims:
        for map_entry in scrim.get("maps", []):
            map_name = (map_entry.get("map_name") or "").strip()
            if map_name not in ATTACK_DEFENSE_MAPS:
                continue

            our_atk_raw = map_entry.get("our_attack_score", "")
            enemy_atk_raw = map_entry.get("enemy_attack_score", "")
            if our_atk_raw in ("", None) or enemy_atk_raw in ("", None):
                continue
            try:
                our_atk = int(our_atk_raw)
                enemy_atk = int(enemy_atk_raw)
            except (ValueError, TypeError):
                continue

            atk_won = our_atk >= 3
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in ("team1", "team2"):
                our_team_slot = "team1"

            for section in map_entry.get("comp", []):
                if (section.get("side") or "").strip() != "Attack":
                    continue
                for slot in section.get(our_team_slot, []):
                    hero = (slot.get("hero") or "").strip()
                    player = (slot.get("player") or "").strip()
                    if not hero or not player:
                        continue
                    player_atk_history[player].append((hero, atk_won))

    # Pivot = hero change after a loss.
    # Per-player aggregates.
    per_player: dict[str, dict] = {}
    # Per (from_hero → to_hero) pair aggregates.
    pair_stats: dict[tuple[str, str], dict] = defaultdict(lambda: {"attempts": 0, "wins": 0, "players": set()})

    for player, history in player_atk_history.items():
        p_attempts = 0
        p_wins = 0
        # Also track which pairs this player used.
        for i in range(1, len(history)):
            prev_hero, prev_won = history[i - 1]
            curr_hero, curr_won = history[i]
            if not prev_won and curr_hero != prev_hero:
                # This is a pivot.
                p_attempts += 1
                if curr_won:
                    p_wins += 1
                pair_stats[(prev_hero, curr_hero)]["attempts"] += 1
                if curr_won:
                    pair_stats[(prev_hero, curr_hero)]["wins"] += 1
                pair_stats[(prev_hero, curr_hero)]["players"].add(player)
        if p_attempts == 0:
            continue
        per_player[player] = {
            "player": player,
            "pivot_attempts": p_attempts,
            "pivot_wins": p_wins,
            "pivot_wr": round(p_wins / p_attempts * 100, 1),
        }

    per_player_rows = sorted(per_player.values(), key=lambda x: -x["pivot_attempts"])

    per_pair_rows = []
    for (from_hero, to_hero), stats in pair_stats.items():
        per_pair_rows.append({
            "from_hero": from_hero,
            "to_hero": to_hero,
            "attempts": stats["attempts"],
            "wins": stats["wins"],
            "win_rate": round(stats["wins"] / stats["attempts"] * 100, 1) if stats["attempts"] else 0,
            "players": sorted(stats["players"]),
        })
    per_pair_rows.sort(key=lambda x: (-x["attempts"], x["from_hero"].lower()))

    total_attempts = sum(p["pivot_attempts"] for p in per_player.values())
    total_wins = sum(p["pivot_wins"] for p in per_player.values())

    return {
        "total_attempts": total_attempts,
        "total_wins": total_wins,
        "overall_wr": round(total_wins / total_attempts * 100, 1) if total_attempts else 0,
        "per_player": per_player_rows,
        "per_pair": per_pair_rows,
    }


def build_atk_def_wr(team_scrims: list[dict]) -> dict:
    """Compute attack/defense round win-rate stats for Escort and Hybrid maps."""
    rounds = 0
    total_atk_score = 0
    total_def_conceded = 0
    atk_wins = 0
    atk_losses = 0
    atk_draws = 0
    full_clears = 0
    full_holds = 0

    per_map: dict[str, dict] = defaultdict(lambda: {
        "rounds": 0, "total_atk": 0, "total_def": 0,
        "wins": 0, "losses": 0, "draws": 0, "full_clears": 0, "full_holds": 0,
    })
    per_hero: dict[str, dict] = defaultdict(lambda: {
        "atk_apps": 0, "atk_wins": 0, "def_apps": 0, "def_wins": 0,
    })

    for scrim in team_scrims:
        for map_entry in scrim.get("maps", []):
            map_name = (map_entry.get("map_name") or "").strip()
            if map_name not in ATTACK_DEFENSE_MAPS:
                continue

            our_atk_raw = map_entry.get("our_attack_score", "")
            enemy_atk_raw = map_entry.get("enemy_attack_score", "")
            if our_atk_raw == "" or our_atk_raw is None or enemy_atk_raw == "" or enemy_atk_raw is None:
                continue
            try:
                our_atk = int(our_atk_raw)
                enemy_atk = int(enemy_atk_raw)
            except (ValueError, TypeError):
                continue

            rounds += 1
            total_atk_score += our_atk
            total_def_conceded += enemy_atk
            pm = per_map[map_name]
            pm["rounds"] += 1
            pm["total_atk"] += our_atk
            pm["total_def"] += enemy_atk

            if our_atk > enemy_atk:
                atk_wins += 1
                pm["wins"] += 1
            elif our_atk < enemy_atk:
                atk_losses += 1
                pm["losses"] += 1
            else:
                atk_draws += 1
                pm["draws"] += 1

            if our_atk >= 3:
                full_clears += 1
                pm["full_clears"] += 1
            if enemy_atk == 0:
                full_holds += 1
                pm["full_holds"] += 1

            # On non-control maps, an attack succeeds when it reaches all 3 checkpoints.
            # A defense succeeds when it prevents the enemy attack from reaching 3.
            round_atk_won = our_atk >= 3
            round_def_won = enemy_atk < 3

            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in ("team1", "team2"):
                our_team_slot = "team1"

            for section in map_entry.get("comp", []):
                section_side = (section.get("side") or "").strip()
                heroes_in_section = [
                    (slot.get("hero") or "").strip()
                    for slot in section.get(our_team_slot, [])
                    if (slot.get("hero") or "").strip()
                ]
                for hero in heroes_in_section:
                    if section_side == "Attack":
                        per_hero[hero]["atk_apps"] += 1
                        if round_atk_won:
                            per_hero[hero]["atk_wins"] += 1
                    elif section_side == "Defense":
                        per_hero[hero]["def_apps"] += 1
                        if round_def_won:
                            per_hero[hero]["def_wins"] += 1

    per_map_rows = []
    for map_name, stats in per_map.items():
        r = stats["rounds"]
        decided = stats["wins"] + stats["losses"]
        per_map_rows.append({
            "map_name": map_name,
            "rounds": r,
            "atk_avg": round(stats["total_atk"] / r, 2) if r else 0,
            "def_avg": round(stats["total_def"] / r, 2) if r else 0,
            "wins": stats["wins"],
            "losses": stats["losses"],
            "draws": stats["draws"],
            "win_rate": round(stats["wins"] / decided * 100, 1) if decided else 0,
            "full_clear_rate": round(stats["full_clears"] / r * 100, 1) if r else 0,
            "full_hold_rate": round(stats["full_holds"] / r * 100, 1) if r else 0,
        })
    per_map_rows.sort(key=lambda x: (-x["rounds"], x["map_name"].lower()))

    per_hero_rows = []
    for hero, stats in per_hero.items():
        total_apps = stats["atk_apps"] + stats["def_apps"]
        if total_apps == 0:
            continue
        per_hero_rows.append({
            "hero": hero,
            "atk_apps": stats["atk_apps"],
            "atk_win_rate": round(stats["atk_wins"] / stats["atk_apps"] * 100, 1) if stats["atk_apps"] else None,
            "def_apps": stats["def_apps"],
            "def_win_rate": round(stats["def_wins"] / stats["def_apps"] * 100, 1) if stats["def_apps"] else None,
        })
    per_hero_rows.sort(key=lambda x: -(x["atk_apps"] + x["def_apps"]))

    decided = atk_wins + atk_losses
    return {
        "rounds": rounds,
        "atk_avg": round(total_atk_score / rounds, 2) if rounds else 0,
        "def_avg": round(total_def_conceded / rounds, 2) if rounds else 0,
        "wins": atk_wins,
        "losses": atk_losses,
        "draws": atk_draws,
        "win_rate": round(atk_wins / decided * 100, 1) if decided else 0,
        "full_clear_rate": round(full_clears / rounds * 100, 1) if rounds else 0,
        "full_hold_rate": round(full_holds / rounds * 100, 1) if rounds else 0,
        "per_map": per_map_rows,
        "per_hero": per_hero_rows,
    }


def build_scrim_log_rows(team_scrims: list) -> dict:
    """Build flat per-map rows for the Scrims tab quick-scan view."""
    _role_order = {"Vanguard": 0, "Duelist": 1, "Strategist": 2}

    def _sort_heroes(raw_heroes: list[str]) -> list[str]:
        unique_by_key: dict[str, str] = {}
        for raw_hero in raw_heroes:
            canonical = _canonical_draft_hero(raw_hero)
            key = _hero_match_key(canonical)
            if not key:
                continue
            unique_by_key[key] = canonical
        return sorted(
            unique_by_key.values(),
            key=lambda h: (_role_order.get(_hero_role(h), 99), h.lower()),
        )

    rows: list[dict] = []
    opponents: set[str] = set()
    all_maps: set[str] = set()
    all_bans: set[str] = set()
    all_duelists: set[str] = set()
    all_seasons: set[str] = set()

    for scrim in team_scrims:
        scrim_id = scrim.get("id")
        scrim_date = (scrim.get("scrim_date", "") or "").strip()
        opponent_name = (
            (scrim.get("enemy_team", "") or "").strip()
            or (scrim.get("opponent", "") or "").strip()
            or "Opponent"
        )
        season = (scrim.get("season", "") or "").strip()
        opponents.add(opponent_name)
        if season:
            all_seasons.add(season)

        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            enemy_team_slot = opposite_team_slot(our_team_slot)

            map_name = (map_entry.get("map_name", "") or "").strip() or "Unknown Map"
            map_type = (map_entry.get("map_type", "Standard") or "Standard").strip()
            result = get_map_outcome_for_slot(map_entry, our_team_slot)
            score = (map_entry.get("score", "") or "").strip()

            draft = map_entry.get("draft", {})
            our_draft = draft.get(our_team_slot, {}) if isinstance(draft, dict) else {}
            enemy_draft = draft.get(enemy_team_slot, {}) if isinstance(draft, dict) else {}

            our_bans = [
                h for h in [
                    (our_draft.get("ban1", "") or "").strip(),
                    (our_draft.get("ban2", "") or "").strip(),
                    (our_draft.get("ban3", "") or "").strip(),
                    (our_draft.get("ban4", "") or "").strip(),
                ] if h
            ]
            enemy_bans = [
                h for h in [
                    (enemy_draft.get("ban1", "") or "").strip(),
                    (enemy_draft.get("ban2", "") or "").strip(),
                    (enemy_draft.get("ban3", "") or "").strip(),
                    (enemy_draft.get("ban4", "") or "").strip(),
                ] if h
            ]
            our_protects = [
                h for h in [
                    (our_draft.get("protect1", "") or "").strip(),
                    (our_draft.get("protect2", "") or "").strip(),
                ] if h
            ]
            enemy_protects = [
                h for h in [
                    (enemy_draft.get("protect1", "") or "").strip(),
                    (enemy_draft.get("protect2", "") or "").strip(),
                ] if h
            ]
            all_bans.update(our_bans)
            all_bans.update(enemy_bans)

            our_raw: list[str] = []
            enemy_raw: list[str] = []
            for section in map_entry.get("comp", []):
                our_raw.extend(
                    (slot.get("hero", "") or "").strip()
                    for slot in section.get(our_team_slot, [])
                    if (slot.get("hero", "") or "").strip()
                )
                enemy_raw.extend(
                    (slot.get("hero", "") or "").strip()
                    for slot in section.get(enemy_team_slot, [])
                    if (slot.get("hero", "") or "").strip()
                )

            our_heroes = _sort_heroes(our_raw)
            enemy_heroes = _sort_heroes(enemy_raw)
            our_duelists = [h for h in our_heroes if _hero_role(h) == "Duelist"]
            all_duelists.update(our_duelists)
            all_maps.add(map_name)

            # Build per-section (round) breakdown
            sections_data = []
            for sec_idx, section in enumerate(map_entry.get("comp", []), start=1):
                sec_label = (section.get("submap", "") or "").strip() or f"Round {sec_idx}"
                sec_score = (section.get("score", "") or "").strip()
                sec_result_raw = (section.get("result", "") or "").strip()
                sec_side = (section.get("side", "") or "").strip()
                # Derive result from score first (most reliable), otherwise
                # flip the stored result when our team is team2 (stored as team1-perspective).
                sec_result = infer_result_from_score_text(sec_score, slot=our_team_slot)
                if not sec_result and sec_result_raw in ("Win", "Loss"):
                    if our_team_slot == "team2":
                        sec_result = "Loss" if sec_result_raw == "Win" else "Win"
                    else:
                        sec_result = sec_result_raw
                elif not sec_result:
                    sec_result = sec_result_raw
                our_slots = [
                    {"hero": (s.get("hero", "") or "").strip(), "player": (s.get("player", "") or "").strip()}
                    for s in section.get(our_team_slot, [])
                ]
                enemy_slots = [
                    {"hero": (s.get("hero", "") or "").strip(), "player": (s.get("player", "") or "").strip()}
                    for s in section.get(enemy_team_slot, [])
                ]
                # Flip score to always show us-first when we are team2
                display_score = sec_score
                if our_team_slot == "team2" and sec_score:
                    left, right = split_score_pair(sec_score)
                    if left and right:
                        display_score = f"{right}-{left}"
                sections_data.append({
                    "label": sec_label,
                    "score": display_score,
                    "result": sec_result,
                    "side": sec_side,
                    "our_slots": our_slots,
                    "enemy_slots": enemy_slots,
                })

            rows.append({
                "scrim_id": scrim_id,
                "scrim_date": scrim_date,
                "opponent_name": opponent_name,
                "season": season,
                "patch": season,
                "map_name": map_name,
                "map_type": map_type,
                "result": result,
                "score": score,
                "our_team_slot": our_team_slot,
                "our_bans": our_bans,
                "our_protects": our_protects,
                "enemy_bans": enemy_bans,
                "enemy_protects": enemy_protects,
                "our_heroes": our_heroes,
                "enemy_heroes": enemy_heroes,
                "our_duelists": our_duelists,
                "sections": sections_data,
            })

    rows.sort(
        key=lambda r: (r.get("scrim_date", ""), r.get("opponent_name", "").lower()),
        reverse=True,
    )

    return {
        "rows": rows,
        "filter_options": {
            "opponents": sorted(opponents),
            "maps": sorted(all_maps),
            "bans": sorted(all_bans),
            "duelists": sorted(all_duelists),
            "seasons": sorted(all_seasons),
        },
    }


def filter_scrim_log_rows(
    rows: list[dict],
    *,
    opponent: str = "",
    map_name: str = "",
    ban: str = "",
    duelist: str = "",
) -> list[dict]:
    selected_opponent = (opponent or "").strip()
    selected_map = (map_name or "").strip()
    selected_ban = (ban or "").strip()
    selected_duelist = (duelist or "").strip()

    filtered_rows: list[dict] = []
    for row in rows:
        if selected_opponent and row.get("opponent_name", "") != selected_opponent:
            continue
        if selected_map and row.get("map_name", "") != selected_map:
            continue
        if selected_ban and selected_ban not in row.get("our_bans", []) + row.get("enemy_bans", []):
            continue
        if selected_duelist and selected_duelist not in row.get("our_duelists", []):
            continue
        filtered_rows.append(row)

    return filtered_rows


def build_scrim_log_export_archive(team_name: str, rows: list[dict]) -> bytes:
    def _winner_label(result: str, our_label: str, their_label: str) -> str:
        if result == "Win":
            return our_label
        if result == "Loss":
            return their_label
        return ""

    def _padded_values(values: list[str], target_size: int) -> list[str]:
        cleaned = [(value or "").strip() for value in values if (value or "").strip()]
        return cleaned[:target_size] + [""] * max(0, target_size - len(cleaned))

    def _draft_action_rows(match_id: str, row: dict) -> list[list[str]]:
        our_team_slot = normalize_match_team_slot(row.get("our_team_slot", "team1"))
        their_team_slot = opposite_team_slot(our_team_slot)
        our_bans = _padded_values(row.get("our_bans", []), 4)
        their_bans = _padded_values(row.get("enemy_bans", []), 4)
        our_protects = _padded_values(row.get("our_protects", []), 2)
        their_protects = _padded_values(row.get("enemy_protects", []), 2)
        slot_sources = {
            f"{our_team_slot}_ban1": our_bans[0],
            f"{our_team_slot}_ban2": our_bans[1],
            f"{our_team_slot}_ban3": our_bans[2],
            f"{our_team_slot}_ban4": our_bans[3],
            f"{our_team_slot}_protect1": our_protects[0],
            f"{our_team_slot}_protect2": our_protects[1],
            f"{their_team_slot}_ban1": their_bans[0],
            f"{their_team_slot}_ban2": their_bans[1],
            f"{their_team_slot}_ban3": their_bans[2],
            f"{their_team_slot}_ban4": their_bans[3],
            f"{their_team_slot}_protect1": their_protects[0],
            f"{their_team_slot}_protect2": their_protects[1],
        }
        team_labels = {our_team_slot: "Our", their_team_slot: "Their"}
        rows_out: list[list[str]] = []
        for order_index, slot_name in enumerate(SIMULATOR_SLOT_ORDER, start=1):
            side_name, action_name = slot_name.split("_", 1)
            hero_name = (slot_sources.get(slot_name, "") or "").strip()
            if not hero_name:
                continue
            action_type = "Protect" if action_name.startswith("protect") else "Ban"
            rows_out.append([
                match_id,
                str(order_index),
                team_labels.get(side_name, side_name.title()),
                action_type,
                hero_name,
            ])
        return rows_out

    def _player_hero_rows(match_id: str, team_side: str, slots: list[dict]) -> list[list[str]]:
        rows_out: list[list[str]] = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            player_name = (slot.get("player", "") or "").strip()
            hero_name = (slot.get("hero", "") or "").strip()
            if not player_name and not hero_name:
                continue
            rows_out.append([match_id, team_side, player_name, hero_name])
        return rows_out

    maps_buffer = io.StringIO(newline="")
    maps_writer = csv.writer(maps_buffer)
    maps_writer.writerow([
        "match_id",
        "date",
        "our_team",
        "their_team",
        "patch",
        "map",
        "map_type",
        "round",
        "map_winner",
        "map_result",
        "map_score",
        "round_winner",
        "round_result",
        "round_score",
        "round_side",
    ])

    draft_buffer = io.StringIO(newline="")
    draft_writer = csv.writer(draft_buffer)
    draft_writer.writerow(["match_id", "action_order", "acting_team", "action_type", "hero"])

    player_buffer = io.StringIO(newline="")
    player_writer = csv.writer(player_buffer)
    player_writer.writerow(["match_id", "team_side", "player", "hero"])

    for row_index, row in enumerate(rows, start=1):
        our_team_name = (team_name or "").strip() or "Our Team"
        their_team_name = (row.get("opponent_name", "") or "").strip() or "Their Team"
        map_result = (row.get("result", "") or "").strip()
        map_score = (row.get("score", "") or "").strip()
        sections = row.get("sections", [])
        if sections:
            for section_index, section in enumerate(sections, start=1):
                match_id = f"S{row.get('scrim_id') or 'x'}-M{row_index}-R{section_index}"
                round_result = (section.get("result", "") or "").strip()
                maps_writer.writerow([
                    match_id,
                    (row.get("scrim_date", "") or "").strip(),
                    our_team_name,
                    their_team_name,
                    (row.get("patch", row.get("season", "")) or "").strip(),
                    (row.get("map_name", "") or "").strip(),
                    (row.get("map_type", "") or "").strip(),
                    (section.get("label", "") or "").strip(),
                    _winner_label(map_result, our_team_name, their_team_name),
                    map_result,
                    map_score,
                    _winner_label(round_result, our_team_name, their_team_name),
                    round_result,
                    (section.get("score", "") or "").strip(),
                    (section.get("side", "") or "").strip(),
                ])
                for draft_row in _draft_action_rows(match_id, row):
                    draft_writer.writerow(draft_row)
                for assignment_row in _player_hero_rows(match_id, "Our", section.get("our_slots", [])):
                    player_writer.writerow(assignment_row)
                for assignment_row in _player_hero_rows(match_id, "Their", section.get("enemy_slots", [])):
                    player_writer.writerow(assignment_row)
        else:
            match_id = f"S{row.get('scrim_id') or 'x'}-M{row_index}-R0"
            maps_writer.writerow([
                match_id,
                (row.get("scrim_date", "") or "").strip(),
                our_team_name,
                their_team_name,
                (row.get("patch", row.get("season", "")) or "").strip(),
                (row.get("map_name", "") or "").strip(),
                (row.get("map_type", "") or "").strip(),
                "",
                _winner_label(map_result, our_team_name, their_team_name),
                map_result,
                map_score,
                "",
                "",
                "",
                "",
            ])
            for draft_row in _draft_action_rows(match_id, row):
                draft_writer.writerow(draft_row)
            for hero_name in row.get("our_heroes", []):
                player_writer.writerow([match_id, "Our", "", (hero_name or "").strip()])
            for hero_name in row.get("enemy_heroes", []):
                player_writer.writerow([match_id, "Their", "", (hero_name or "").strip()])

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("maps.csv", "\ufeff" + maps_buffer.getvalue())
        archive.writestr("draft_actions.csv", "\ufeff" + draft_buffer.getvalue())
        archive.writestr("player_heroes.csv", "\ufeff" + player_buffer.getvalue())

    return archive_buffer.getvalue()


@app.route("/teams/<int:team_id>/scrims.csv")
def team_scrims_csv(team_id: int):
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    all_team_scrims = get_scrims_for_team(team["id"], team["name"])
    season_options = get_scrim_season_options(all_team_scrims)
    default_season = get_current_season_from_recent_scrim(all_team_scrims)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in all_team_scrims)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )
    selected_map_type = get_selected_map_type(request.args.get("map_type", "all"))
    team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
    team_scrims = filter_scrims_by_map_type(team_scrims, selected_map_type)

    scrim_log = build_scrim_log_rows(team_scrims)
    filtered_rows = filter_scrim_log_rows(
        scrim_log["rows"],
        opponent=request.args.get("opponent", ""),
        map_name=request.args.get("map", ""),
        ban=request.args.get("ban", ""),
        duelist=request.args.get("duelist", ""),
    )

    filename_parts = [secure_filename((team["name"] or "team").strip()) or f"team-{team_id}", "scrims"]
    if selected_season and selected_season != "all":
        filename_parts.append(f"season-{selected_season}")
    if selected_map_type and selected_map_type != "all":
        filename_parts.append(secure_filename(selected_map_type.lower()))
    archive_bytes = build_scrim_log_export_archive(team["name"], filtered_rows)
    filename = "-".join(filename_parts) + ".zip"

    return Response(
        archive_bytes,
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/teams/<int:team_id>")
def team_detail(team_id: int):
    db = get_db()
    migrate_enemy_teams_to_team_database(db)
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    all_team_scrims = get_scrims_for_team(team["id"], team["name"])
    season_options = get_scrim_season_options(all_team_scrims)
    default_season = get_current_season_from_recent_scrim(all_team_scrims)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in all_team_scrims)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )
    selected_map_type = get_selected_map_type(request.args.get("map_type", "all"))
    team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
    team_scrims = filter_scrims_by_map_type(team_scrims, selected_map_type)
    predictor_inputs = {
        field_key: (request.args.get(field_key) or "").strip()
        for field_key in PREDICTOR_INPUT_ORDER
    }
    draft_predictor = build_draft_predictor(team_scrims, predictor_inputs)
    team_tournament_rows = build_team_tournament_rows(team)

    team_analytics = build_scrim_analytics(
        team_scrims,
        roster_player_names=[row["name"] for row in db.execute(
            "SELECT name FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
            (team_id,),
        ).fetchall()],
    )

    map_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    mode_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    map_type_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    opponent_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    recent_map_visual_rows: list[dict] = []
    map_timeline_targets: dict[str, int] = {}

    for scrim in team_scrims:
        for map_entry in scrim.get("maps", []):
            map_name = (map_entry.get("map_name", "") or "").strip()
            if not map_name:
                continue

            if map_name not in map_timeline_targets and scrim.get("id") is not None:
                map_timeline_targets[map_name] = scrim.get("id")

            mode_name = MAP_MODES.get(map_name, "Other")
            map_type_name = normalize_map_type_value(map_entry.get("map_type", ""))
            outcome = get_map_outcome_for_slot(map_entry, map_entry.get("our_team_slot", "team1"))
            opponent_name = (
                (scrim.get("enemy_team", "") or "").strip()
                or (scrim.get("opponent", "") or "").strip()
                or "Opponent"
            )

            map_records[map_name]["maps"] += 1
            mode_records[mode_name]["maps"] += 1
            map_type_records[map_type_name]["maps"] += 1
            opponent_records[opponent_name]["maps"] += 1

            recent_map_visual_rows.append(
                {
                    "scrim_date": scrim.get("scrim_date", ""),
                    "map_name": map_name,
                    "mode": mode_name,
                    "map_type": map_type_name,
                    "outcome": outcome,
                    "opponent": opponent_name,
                }
            )

            if outcome == "Win":
                map_records[map_name]["wins"] += 1
                map_records[map_name]["decided"] += 1
                mode_records[mode_name]["wins"] += 1
                mode_records[mode_name]["decided"] += 1
                map_type_records[map_type_name]["wins"] += 1
                map_type_records[map_type_name]["decided"] += 1
                opponent_records[opponent_name]["wins"] += 1
                opponent_records[opponent_name]["decided"] += 1
            elif outcome == "Loss":
                map_records[map_name]["losses"] += 1
                map_records[map_name]["decided"] += 1
                mode_records[mode_name]["losses"] += 1
                mode_records[mode_name]["decided"] += 1
                map_type_records[map_type_name]["losses"] += 1
                map_type_records[map_type_name]["decided"] += 1
                opponent_records[opponent_name]["losses"] += 1
                opponent_records[opponent_name]["decided"] += 1
            else:
                map_records[map_name]["unresolved"] += 1
                mode_records[mode_name]["unresolved"] += 1
                map_type_records[map_type_name]["unresolved"] += 1
                opponent_records[opponent_name]["unresolved"] += 1

    team_map_cards = []
    for map_name, stats in map_records.items():
        maps_played = stats["maps"]
        decided_maps = stats["decided"]
        win_rate = round((stats["wins"] / decided_maps) * 100, 1) if decided_maps else 0
        team_map_cards.append(
            {
                "map_name": map_name,
                "mode": MAP_MODES.get(map_name, "Other"),
                "maps": maps_played,
                "decided_maps": decided_maps,
                "unresolved_maps": stats["unresolved"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": win_rate,
                "image": MAP_IMAGES.get(map_name, ""),
                "timeline_scrim_id": map_timeline_targets.get(map_name),
            }
        )
    team_map_cards.sort(key=lambda r: (r["win_rate"], r["maps"]), reverse=True)

    team_map_mode_rows = []
    for mode_name, stats in mode_records.items():
        maps_played = stats["maps"]
        decided_maps = stats["decided"]
        win_rate = round((stats["wins"] / decided_maps) * 100, 1) if decided_maps else 0
        mode_maps = [card for card in team_map_cards if card["mode"] == mode_name]
        best_map = max(mode_maps, key=lambda m: (m["win_rate"], m["maps"]), default=None)
        worst_map = min(mode_maps, key=lambda m: (m["win_rate"], -m["maps"]), default=None)
        team_map_mode_rows.append(
            {
                "mode": mode_name,
                "maps": maps_played,
                "decided_maps": decided_maps,
                "unresolved_maps": stats["unresolved"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": win_rate,
                "best_map": best_map,
                "worst_map": worst_map,
            }
        )
    team_map_mode_rows.sort(key=lambda r: (r["win_rate"], r["maps"]), reverse=True)

    best_mode = team_map_mode_rows[0] if team_map_mode_rows else None
    worst_mode = team_map_mode_rows[-1] if team_map_mode_rows else None

    map_type_visual_rows = []
    for map_type in MAP_TYPES:
        stats = map_type_records.get(map_type)
        if not stats or not stats["maps"]:
            continue
        decided_maps = stats["decided"]
        win_rate = round((stats["wins"] / decided_maps) * 100, 1) if decided_maps else 0
        map_type_visual_rows.append(
            {
                "map_type": map_type,
                "maps": stats["maps"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "unresolved": stats["unresolved"],
                "win_rate": win_rate,
            }
        )

    total_maps_for_type_visual = sum(row["maps"] for row in map_type_visual_rows)
    for row in map_type_visual_rows:
        row["share"] = round((row["maps"] / total_maps_for_type_visual) * 100, 1) if total_maps_for_type_visual else 0
    map_type_visual_rows.sort(key=lambda row: (row["maps"], row["win_rate"]), reverse=True)

    opponent_visual_rows = []
    for opponent_name, stats in opponent_records.items():
        decided_maps = stats["decided"]
        win_rate = round((stats["wins"] / decided_maps) * 100, 1) if decided_maps else 0
        opponent_visual_rows.append(
            {
                "opponent": opponent_name,
                "maps": stats["maps"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "unresolved": stats["unresolved"],
                "win_rate": win_rate,
            }
        )
    opponent_visual_rows.sort(key=lambda row: (row["maps"], row["win_rate"]), reverse=True)
    opponent_visual_rows = opponent_visual_rows[:8]

    recent_map_visual_rows = list(reversed(recent_map_visual_rows[-24:]))

    player_rows = db.execute(
        "SELECT * FROM players WHERE team_id = ? ORDER BY is_sub ASC, name COLLATE NOCASE",
        (team_id,),
    ).fetchall()

    players = []
    for row in player_rows:
        stats = compute_player_stats(row["name"], team_scrims)
        player_breakdown = build_player_hero_map_breakdown(row["name"], team_scrims)
        primary_hero = player_breakdown["hero_rows"][0]["hero"] if player_breakdown["hero_rows"] else ""
        players.append({
            "id": row["id"],
            "name": row["name"],
            "role": row["role"],
            "is_sub": bool(row["is_sub"]) if "is_sub" in row.keys() else False,
            "main_hero": row["main_hero"],
            "top_hero": primary_hero,
            "notes": row["notes"],
            "stats": stats,
            "hero_rows": player_breakdown.get("hero_rows", []),
        })

    team_hero_profile = build_team_hero_profile(team_scrims, players)
    hero_graph_rows = team_hero_profile.get("top_heroes", [])
    hero_usage_timeline = build_hero_usage_timeline(
        team_scrims,
        [row["hero"] for row in hero_graph_rows[:6]],
    )

    team_ban_impact = build_team_ban_impact(team_scrims)

    atk_def_wr = build_atk_def_wr(team_scrims)
    pivot_wr = build_pivot_wr(team_scrims)
    scrim_log = build_scrim_log_rows(team_scrims)
    # Enrich team_map_cards with per-map attack/defense averages
    _atk_def_by_map = {row["map_name"]: row for row in atk_def_wr["per_map"]}
    for _card in team_map_cards:
        _stats = _atk_def_by_map.get(_card["map_name"])
        _card["attack_score_avg"] = _stats["atk_avg"] if _stats else None
        _card["defense_score_avg"] = _stats["def_avg"] if _stats else None

    role_order = {"Vanguard": 0, "Duelist": 1, "Strategist": 2}

    def _sorted_heroes_for_matchup(raw_heroes: list[str]) -> list[str]:
        unique_by_key: dict[str, str] = {}
        for raw_hero in raw_heroes:
            canonical = _canonical_draft_hero(raw_hero)
            key = _hero_match_key(canonical)
            if not key:
                continue
            unique_by_key[key] = canonical

        return sorted(
            unique_by_key.values(),
            key=lambda hero_name: (
                role_order.get(_hero_role(hero_name), 99),
                hero_name.lower(),
            ),
        )

    matchup_rows = []
    matchup_opponents = set()
    matchup_maps = set()
    matchup_map_totals = defaultdict(int)
    matchup_wins = 0
    matchup_losses = 0
    matchup_other_results = 0

    for scrim in team_scrims:
        scrim_date = (scrim.get("scrim_date", "") or "").strip()
        opponent_name = (
            (scrim.get("enemy_team", "") or "").strip()
            or (scrim.get("opponent", "") or "").strip()
            or "Opponent"
        )
        matchup_opponents.add(opponent_name)

        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            enemy_team_slot = opposite_team_slot(our_team_slot)

            map_name = (map_entry.get("map_name", "") or "").strip() or "Unknown Map"
            result = get_map_outcome_for_slot(map_entry, our_team_slot)

            if result == "Win":
                matchup_wins += 1
            elif result == "Loss":
                matchup_losses += 1
            else:
                matchup_other_results += 1

            matchup_maps.add(map_name)
            matchup_map_totals[map_name] += 1

            our_raw_heroes = []
            enemy_raw_heroes = []
            for section in map_entry.get("comp", []):
                our_raw_heroes.extend(
                    [
                        (slot.get("hero", "") or "").strip()
                        for slot in section.get(our_team_slot, [])
                        if (slot.get("hero", "") or "").strip()
                    ]
                )
                enemy_raw_heroes.extend(
                    [
                        (slot.get("hero", "") or "").strip()
                        for slot in section.get(enemy_team_slot, [])
                        if (slot.get("hero", "") or "").strip()
                    ]
                )

            matchup_rows.append(
                {
                    "scrim_date": scrim_date,
                    "opponent_name": opponent_name,
                    "map_name": map_name,
                    "result": result,
                    "our_heroes": _sorted_heroes_for_matchup(our_raw_heroes),
                    "enemy_heroes": _sorted_heroes_for_matchup(enemy_raw_heroes),
                }
            )

    matchup_rows.sort(
        key=lambda row: (
            row.get("scrim_date", ""),
            row.get("opponent_name", "").lower(),
            row.get("map_name", "").lower(),
        ),
        reverse=True,
    )

    matchup_summary = {
        "total_maps": len(matchup_rows),
        "wins": matchup_wins,
        "losses": matchup_losses,
        "other_results": matchup_other_results,
        "decided_maps": matchup_wins + matchup_losses,
        "win_rate": round((matchup_wins / (matchup_wins + matchup_losses)) * 100, 1) if (matchup_wins + matchup_losses) else 0,
        "unique_opponents": len(matchup_opponents),
        "unique_maps": len(matchup_maps),
    }

    matrix_map_columns = [
        map_name
        for map_name, _count in sorted(
            matchup_map_totals.items(),
            key=lambda item: (-item[1], item[0].lower()),
        )
    ]
    matrix_rows = []
    for player in players:
        per_map = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
        for scrim in team_scrims:
            for map_entry in scrim.get("maps", []):
                our_team_slot = map_entry.get("our_team_slot", "team1")
                if our_team_slot not in TEAM_SLOTS:
                    our_team_slot = "team1"

                player_found = False
                for section in map_entry.get("comp", []):
                    for slot in section.get(our_team_slot, []):
                        if (slot.get("player", "") or "").strip().lower() == player["name"].strip().lower():
                            player_found = True
                            break
                    if player_found:
                        break

                if not player_found:
                    continue

                map_name = (map_entry.get("map_name", "") or "").strip() or "Unknown Map"
                result = get_map_outcome_for_slot(map_entry, our_team_slot)
                per_map[map_name]["maps"] += 1
                if result == "Win":
                    per_map[map_name]["wins"] += 1
                    per_map[map_name]["decided"] += 1
                elif result == "Loss":
                    per_map[map_name]["losses"] += 1
                    per_map[map_name]["decided"] += 1
                else:
                    per_map[map_name]["unresolved"] += 1

        cells = []
        total_maps = 0
        total_wins = 0
        total_losses = 0
        total_decided = 0
        total_unresolved = 0
        for map_name in matrix_map_columns:
            stats = per_map.get(map_name)
            if not stats or not stats["maps"]:
                cells.append(None)
                continue

            total_maps += stats["maps"]
            total_wins += stats["wins"]
            total_losses += stats["losses"]
            total_decided += stats["decided"]
            total_unresolved += stats["unresolved"]
            cells.append(
                {
                    "maps": stats["maps"],
                    "decided_maps": stats["decided"],
                    "unresolved_maps": stats["unresolved"],
                    "wins": stats["wins"],
                    "losses": stats["losses"],
                    "win_rate": round((stats["wins"] / stats["decided"]) * 100, 1) if stats["decided"] else 0,
                }
            )

        matrix_rows.append(
            {
                "player_id": player["id"],
                "player_name": player["name"],
                "role": player.get("role", ""),
                "cells": cells,
                "summary": {
                    "maps": total_maps,
                    "decided_maps": total_decided,
                    "unresolved_maps": total_unresolved,
                    "wins": total_wins,
                    "losses": total_losses,
                    "win_rate": round((total_wins / total_decided) * 100, 1) if total_decided else 0,
                },
            }
        )

    matrix_rows.sort(
        key=lambda row: (
            role_order.get((row.get("role", "") or "").strip(), 99),
            -row["summary"]["maps"],
            row["player_name"].lower(),
        )
    )

    enemy_team_rows = db.execute(
        "SELECT id, name, notes, logo_path, created_at FROM teams WHERE id != ? ORDER BY name COLLATE NOCASE",
        (team_id,),
    ).fetchall()

    enemy_teams = []
    for enemy_row in enemy_team_rows:
        enemy_players = db.execute(
            "SELECT id, name, role, main_hero, notes FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
            (enemy_row["id"],),
        ).fetchall()
        enemy_teams.append({
            "id": enemy_row["id"],
            "name": enemy_row["name"],
            "notes": enemy_row["notes"],
            "logo_path": enemy_row["logo_path"],
            "created_at": enemy_row["created_at"],
            "players": [dict(p) for p in enemy_players],
        })

    prep_context = build_team_prep_context(
        team_scrims=team_scrims,
        team_players=player_rows,
        enemy_teams=enemy_teams,
        selected_enemy_id_raw=request.args.get("prep_enemy_id", ""),
        compare_map_a_raw=request.args.get("compare_map_a", ""),
        compare_map_b_raw=request.args.get("compare_map_b", ""),
    )

    return render_template(
        "team_detail.html",
        team=team,
        players=players,
        enemy_teams=enemy_teams,
        team_tournament_rows=team_tournament_rows,
        player_roles=PLAYER_ROLES,
        team_analytics=team_analytics,
        season_options=season_options,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
        has_unseasoned_scrims=has_unseasoned_scrims,
        selected_season=selected_season,
        selected_map_type=selected_map_type,
        map_type_options=MAP_TYPES,
        hero_graph_rows=hero_graph_rows,
        hero_usage_timeline=hero_usage_timeline,
        team_scrim_count=len(team_scrims),
        team_scrim_total_count=len(all_team_scrims),
        team_map_cards=team_map_cards,
        team_map_mode_rows=team_map_mode_rows,
        best_mode=best_mode,
        worst_mode=worst_mode,
        map_type_visual_rows=map_type_visual_rows,
        opponent_visual_rows=opponent_visual_rows,
        recent_map_visual_rows=recent_map_visual_rows,
        map_modes=MAP_MODES,
        map_images=MAP_IMAGES,
        draft_predictor=draft_predictor,
        matchup_summary=matchup_summary,
        matchup_rows=matchup_rows,
        player_map_matrix_columns=matrix_map_columns,
        player_map_matrix_rows=matrix_rows,
        team_hero_profile=team_hero_profile,
        hero_transformations=HERO_TRANSFORMATIONS,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
        team_ban_impact=team_ban_impact,
        atk_def_wr=atk_def_wr,
        pivot_wr=pivot_wr,
        scrim_log=scrim_log,
        **prep_context,
    )


@app.route("/tournaments/<int:tournament_id>/teams/<int:tournament_team_id>")
def tournament_team_detail(tournament_id: int, tournament_team_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_team = get_tournament_team_by_id(tournament_record, tournament_team_id)
    if tournament_team is None:
        abort(404)

    team_scrims = build_tournament_team_scrims(tournament_record, tournament_team)
    team_analytics = build_scrim_analytics(team_scrims)
    hero_graph_rows = [
        {
            "hero": row["hero"],
            "maps": row["maps"],
            "pick_rate": round((row["maps"] / team_analytics["summary"]["total_maps"]) * 100, 1)
            if team_analytics["summary"]["total_maps"]
            else 0,
            "unmirrored_win_rate": row["unmirrored_win_rate"],
        }
        for row in team_analytics.get("hero_rows", [])
    ]
    hero_usage_timeline = build_hero_usage_timeline(
        team_scrims,
        [row["hero"] for row in hero_graph_rows[:6]],
    )

    map_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})
    mode_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})
    map_timeline_targets: dict[str, int] = {}
    match_rows = []

    for tournament_match in tournament_record.get("matches", []):
        if tournament_match.get("team1_tournament_team_id") == tournament_team_id:
            opponent_name = tournament_match.get("team2_name") or "Opponent"
        elif tournament_match.get("team2_tournament_team_id") == tournament_team_id:
            opponent_name = tournament_match.get("team1_name") or "Opponent"
        else:
            continue

        wins = 0
        losses = 0
        for map_entry in tournament_match.get("maps", []):
            team_slot = get_tournament_team_slot_for_map(map_entry, tournament_team_id)
            if team_slot is None:
                continue
            map_name = (map_entry.get("map_name", "") or "").strip()
            if map_name:
                mode_name = MAP_MODES.get(map_name, "Other")
                map_records[map_name]["maps"] += 1
                mode_records[mode_name]["maps"] += 1
                if map_name not in map_timeline_targets and tournament_match.get("id") is not None:
                    map_timeline_targets[map_name] = tournament_match.get("id")

            result = get_map_outcome_for_slot(map_entry, team_slot)
            if result == "Win":
                wins += 1
                if map_name:
                    map_records[map_name]["wins"] += 1
                    mode_records[mode_name]["wins"] += 1
            elif result == "Loss":
                losses += 1
                if map_name:
                    map_records[map_name]["losses"] += 1
                    mode_records[mode_name]["losses"] += 1

        match_rows.append(
            {
                "id": tournament_match.get("id"),
                "opponent_name": opponent_name,
                "scrim_date": tournament_match.get("scrim_date") or tournament_record.get("scrim_date", ""),
                "maps": len(tournament_match.get("maps", [])),
                "wins": wins,
                "losses": losses,
            }
        )

    team_map_cards = []
    for map_name, stats in map_records.items():
        maps_played = stats["maps"]
        win_rate = round((stats["wins"] / maps_played) * 100, 1) if maps_played else 0
        team_map_cards.append(
            {
                "map_name": map_name,
                "mode": MAP_MODES.get(map_name, "Other"),
                "maps": maps_played,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": win_rate,
                "image": MAP_IMAGES.get(map_name, ""),
                "timeline_match_id": map_timeline_targets.get(map_name),
            }
        )
    team_map_cards.sort(key=lambda row: (row["win_rate"], row["maps"]), reverse=True)

    team_map_mode_rows = []
    for mode_name, stats in mode_records.items():
        maps_played = stats["maps"]
        win_rate = round((stats["wins"] / maps_played) * 100, 1) if maps_played else 0
        mode_maps = [card for card in team_map_cards if card["mode"] == mode_name]
        best_map = max(mode_maps, key=lambda row: (row["win_rate"], row["maps"]), default=None)
        worst_map = min(mode_maps, key=lambda row: (row["win_rate"], -row["maps"]), default=None)
        team_map_mode_rows.append(
            {
                "mode": mode_name,
                "maps": maps_played,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": win_rate,
                "best_map": best_map,
                "worst_map": worst_map,
            }
        )
    team_map_mode_rows.sort(key=lambda row: (row["win_rate"], row["maps"]), reverse=True)

    picked_map_rows = build_tournament_team_pick_rows(tournament_record, tournament_team)
    players = [
        {
            "name": player_name,
            "stats": compute_player_stats(player_name, team_scrims),
        }
        for player_name in tournament_team.get("players", [])
    ]

    return render_template(
        "tournament_team_detail.html",
        tournament=tournament_record,
        tournament_team=tournament_team,
        team_analytics=team_analytics,
        hero_graph_rows=hero_graph_rows,
        hero_usage_timeline=hero_usage_timeline,
        team_map_cards=team_map_cards,
        team_map_mode_rows=team_map_mode_rows,
        best_mode=team_map_mode_rows[0] if team_map_mode_rows else None,
        worst_mode=team_map_mode_rows[-1] if team_map_mode_rows else None,
        picked_map_rows=picked_map_rows,
        match_rows=match_rows,
        players=players,
        hero_transformations=HERO_TRANSFORMATIONS,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
        map_images=MAP_IMAGES,
    )


@app.route("/teams/<int:team_id>/prep-fragment")
def team_prep_fragment(team_id: int):
    db = get_db()
    migrate_enemy_teams_to_team_database(db)
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    all_team_scrims = get_scrims_for_team(team["id"], team["name"])
    season_options = get_scrim_season_options(all_team_scrims)
    default_season = get_current_season_from_recent_scrim(all_team_scrims)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in all_team_scrims)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )
    selected_map_type = get_selected_map_type(request.args.get("map_type", "all"))
    team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
    team_scrims = filter_scrims_by_map_type(team_scrims, selected_map_type)

    enemy_team_rows = db.execute(
        "SELECT id, name, notes, logo_path, created_at FROM teams WHERE id != ? ORDER BY name COLLATE NOCASE",
        (team_id,),
    ).fetchall()
    enemy_teams = [dict(row) for row in enemy_team_rows]
    player_rows = db.execute(
        "SELECT id, name, role, main_hero, notes FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
        (team_id,),
    ).fetchall()

    prep_context = build_team_prep_context(
        team_scrims=team_scrims,
        team_players=player_rows,
        enemy_teams=enemy_teams,
        selected_enemy_id_raw=request.args.get("prep_enemy_id", ""),
        compare_map_a_raw=request.args.get("compare_map_a", ""),
        compare_map_b_raw=request.args.get("compare_map_b", ""),
    )

    return render_template(
        "_team_prep_content.html",
        team=team,
        enemy_teams=enemy_teams,
        selected_season=selected_season,
        selected_map_type=selected_map_type,
        **prep_context,
    )


@app.route("/teams/<int:team_id>/draft-predict")
def team_draft_predict(team_id: int):
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    all_team_scrims = get_scrims_for_team(team["id"], team["name"])
    season_options = get_scrim_season_options(all_team_scrims)
    default_season = get_current_season_from_recent_scrim(all_team_scrims)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in all_team_scrims)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )
    selected_map_type = get_selected_map_type(request.args.get("map_type", "all"))
    team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
    team_scrims = filter_scrims_by_map_type(team_scrims, selected_map_type)
    predictor_inputs = {
        field_key: (request.args.get(field_key) or "").strip()
        for field_key in PREDICTOR_INPUT_ORDER
    }
    return jsonify(build_draft_predictor(team_scrims, predictor_inputs))


@app.route("/teams/<int:team_id>/heroes/<path:hero_name>")
def team_hero_detail(team_id: int, hero_name: str):
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    target_hero = (hero_name or "").strip()
    if not target_hero:
        abort(404)

    all_team_scrims = get_scrims_for_team(team["id"], team["name"])
    season_options = get_scrim_season_options(all_team_scrims)
    default_season = get_current_season_from_recent_scrim(all_team_scrims)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in all_team_scrims)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )
    selected_map_type = get_selected_map_type(request.args.get("map_type", "all"))
    team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
    team_scrims = filter_scrims_by_map_type(team_scrims, selected_map_type)

    hero_insights = build_team_hero_insights(team_scrims, target_hero)
    if not hero_insights["summary"]["maps_played"]:
        flash(f"No comp data found for {target_hero}.", "error")
        return redirect(url_for("team_detail", team_id=team_id, season=selected_season, map_type=selected_map_type) + "#comps")

    return render_template(
        "hero_detail.html",
        team=team,
        hero_insights=hero_insights,
        map_images=MAP_IMAGES,
        selected_season=selected_season,
        selected_map_type=selected_map_type,
        season_options=season_options,
        has_unseasoned_scrims=has_unseasoned_scrims,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
    )


@app.route("/teams/<int:team_id>/players/<int:player_id>")
def player_detail(team_id: int, player_id: int):
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    player = db.execute(
        "SELECT * FROM players WHERE id = ? AND team_id = ?",
        (player_id, team_id),
    ).fetchone()
    if player is None:
        abort(404)

    all_team_scrims = get_scrims_for_team(team["id"], team["name"])
    season_options = get_scrim_season_options(all_team_scrims)
    default_season = get_current_season_from_recent_scrim(all_team_scrims)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in all_team_scrims)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )
    selected_map_type = get_selected_map_type(request.args.get("map_type", "all"))
    team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
    team_scrims = filter_scrims_by_map_type(team_scrims, selected_map_type)

    player_stats = compute_player_stats(player["name"], team_scrims)
    breakdown = build_player_hero_map_breakdown(player["name"], team_scrims)
    primary_hero_row = breakdown["hero_rows"][0] if breakdown["hero_rows"] else None
    recent_map_rows = build_player_recent_maps(player["name"], team_scrims, limit=20)
    swap_summary = build_player_submap_swap_summary(player["name"], team_scrims, limit=20)
    player_ban_impact = build_player_ban_impact(player["name"], team_scrims)

    best_map_row = max(breakdown["map_rows"], key=lambda row: (row["win_rate"], row["maps"]), default=None)
    worst_map_row = min(breakdown["map_rows"], key=lambda row: (row["win_rate"], -row["maps"]), default=None)
    player_insights = {
        "unique_heroes": len(breakdown["hero_rows"]),
        "primary_hero": primary_hero_row,
        "best_map": best_map_row,
        "worst_map": worst_map_row,
    }

    return render_template(
        "player_detail.html",
        team=team,
        player=player,
        player_stats=player_stats,
        player_hero_rows=breakdown["hero_rows"],
        player_map_rows=breakdown["map_rows"],
        player_recent_maps=recent_map_rows,
        player_swap_summary=swap_summary,
        player_ban_impact=player_ban_impact,
        player_insights=player_insights,
        selected_season=selected_season,
        selected_map_type=selected_map_type,
        map_type_options=MAP_TYPES,
        season_options=season_options,
        has_unseasoned_scrims=has_unseasoned_scrims,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
    )


@app.route("/players/compare")
def player_compare():
    db = get_db()
    migrate_enemy_teams_to_team_database(db)
    player_rows = db.execute(
        """
        SELECT p.id, p.name, p.role, p.main_hero, p.notes, p.team_id, t.name AS team_name
        FROM players p
        JOIN teams t ON t.id = p.team_id
        ORDER BY p.name COLLATE NOCASE
        """
    ).fetchall()

    options = [dict(row) for row in player_rows]
    option_lookup = {str(row["id"]): dict(row) for row in player_rows}

    player_a_id = (request.args.get("player_a") or "").strip()
    player_b_id = (request.args.get("player_b") or "").strip()
    player_a = option_lookup.get(player_a_id)
    player_b = option_lookup.get(player_b_id)

    comparison_scrims: list[dict] = []
    for selected_player in (player_a, player_b):
        if not selected_player:
            continue
        comparison_scrims.extend(get_scrims_for_team(selected_player["team_id"], selected_player.get("team_name", "")))

    season_options = get_scrim_season_options(comparison_scrims)
    default_season = get_current_season_from_recent_scrim(comparison_scrims)
    has_unseasoned_scrims = any(
        not normalize_season_value(scrim.get("season", ""))
        for scrim in comparison_scrims
    )
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )

    def load_player_payload(player_row: dict | None) -> dict | None:
        if player_row is None:
            return None

        team_scrims = get_scrims_for_team(player_row["team_id"], player_row.get("team_name", ""))
        team_scrims = filter_scrims_by_season(team_scrims, selected_season)
        stats = compute_player_stats(player_row["name"], team_scrims)
        breakdown = build_player_hero_map_breakdown(player_row["name"], team_scrims)
        primary_hero = breakdown["hero_rows"][0] if breakdown["hero_rows"] else None
        ban_impact = build_player_ban_impact(player_row["name"], team_scrims)
        recent_maps = build_player_recent_maps(player_row["name"], team_scrims, limit=10)
        return {
            "profile": player_row,
            "stats": stats,
            "primary_hero": primary_hero,
            "hero_rows": breakdown["hero_rows"][:8],
            "map_rows": breakdown["map_rows"][:8],
            "hero_rows_full": breakdown["hero_rows"],
            "map_rows_full": breakdown["map_rows"],
            "ban_impact": ban_impact,
            "recent_maps": recent_maps,
        }

    payload_a = load_player_payload(player_a)
    payload_b = load_player_payload(player_b)

    shared_heroes = []
    shared_maps = []
    hero_winrate_differences = []
    map_winrate_differences = []
    if payload_a and payload_b:
        hero_lookup_a = {row["hero"]: row for row in payload_a["hero_rows_full"]}
        hero_lookup_b = {row["hero"]: row for row in payload_b["hero_rows_full"]}
        for hero_name in sorted(set(hero_lookup_a) & set(hero_lookup_b)):
            shared_heroes.append(
                {
                    "hero": hero_name,
                    "player_a_maps": hero_lookup_a[hero_name]["maps"],
                    "player_b_maps": hero_lookup_b[hero_name]["maps"],
                }
            )
        shared_heroes.sort(key=lambda row: row["player_a_maps"] + row["player_b_maps"], reverse=True)

        map_lookup_a = {row["map_name"]: row for row in payload_a["map_rows_full"]}
        map_lookup_b = {row["map_name"]: row for row in payload_b["map_rows_full"]}
        for map_name in sorted(set(map_lookup_a) & set(map_lookup_b)):
            shared_maps.append(
                {
                    "map_name": map_name,
                    "player_a_maps": map_lookup_a[map_name]["maps"],
                    "player_b_maps": map_lookup_b[map_name]["maps"],
                }
            )
        shared_maps.sort(key=lambda row: row["player_a_maps"] + row["player_b_maps"], reverse=True)

        for hero_name in sorted(set(hero_lookup_a) | set(hero_lookup_b)):
            row_a = hero_lookup_a.get(hero_name)
            row_b = hero_lookup_b.get(hero_name)
            a_maps = int((row_a or {}).get("maps") or 0)
            b_maps = int((row_b or {}).get("maps") or 0)
            a_decided = int((row_a or {}).get("decided_maps") or 0)
            b_decided = int((row_b or {}).get("decided_maps") or 0)
            a_wr = float((row_a or {}).get("win_rate") or 0) if a_decided else None
            b_wr = float((row_b or {}).get("win_rate") or 0) if b_decided else None
            diff = round(a_wr - b_wr, 1) if a_wr is not None and b_wr is not None else None
            hero_winrate_differences.append(
                {
                    "hero": hero_name,
                    "player_a_maps": a_maps,
                    "player_b_maps": b_maps,
                    "player_a_decided_maps": a_decided,
                    "player_b_decided_maps": b_decided,
                    "player_a_win_rate": a_wr,
                    "player_b_win_rate": b_wr,
                    "win_rate_diff": diff,
                }
            )

        hero_winrate_differences.sort(
            key=lambda row: (
                row["win_rate_diff"] is not None,
                abs(row["win_rate_diff"] or 0),
                row["player_a_decided_maps"] + row["player_b_decided_maps"],
                row["player_a_maps"] + row["player_b_maps"],
            ),
            reverse=True,
        )

        for map_name in sorted(set(map_lookup_a) | set(map_lookup_b)):
            row_a = map_lookup_a.get(map_name)
            row_b = map_lookup_b.get(map_name)
            a_maps = int((row_a or {}).get("maps") or 0)
            b_maps = int((row_b or {}).get("maps") or 0)
            a_decided = int((row_a or {}).get("decided_maps") or 0)
            b_decided = int((row_b or {}).get("decided_maps") or 0)
            a_wr = float((row_a or {}).get("win_rate") or 0) if a_decided else None
            b_wr = float((row_b or {}).get("win_rate") or 0) if b_decided else None
            diff = round(a_wr - b_wr, 1) if a_wr is not None and b_wr is not None else None
            map_winrate_differences.append(
                {
                    "map_name": map_name,
                    "player_a_maps": a_maps,
                    "player_b_maps": b_maps,
                    "player_a_decided_maps": a_decided,
                    "player_b_decided_maps": b_decided,
                    "player_a_win_rate": a_wr,
                    "player_b_win_rate": b_wr,
                    "win_rate_diff": diff,
                }
            )

        map_winrate_differences.sort(
            key=lambda row: (
                row["win_rate_diff"] is not None,
                abs(row["win_rate_diff"] or 0),
                row["player_a_decided_maps"] + row["player_b_decided_maps"],
                row["player_a_maps"] + row["player_b_maps"],
            ),
            reverse=True,
        )

    return render_template(
        "player_compare.html",
        player_options=options,
        selected_player_a_id=player_a_id,
        selected_player_b_id=player_b_id,
        player_a=payload_a,
        player_b=payload_b,
        shared_heroes=shared_heroes[:10],
        shared_maps=shared_maps[:10],
        hero_winrate_differences=hero_winrate_differences[:20],
        map_winrate_differences=map_winrate_differences[:20],
        selected_season=selected_season,
        season_options=season_options,
        has_unseasoned_scrims=has_unseasoned_scrims,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
    )


@app.route("/teams/<int:team_id>/players/create", methods=["POST"])
def create_player(team_id: int):
    db = get_db()
    team_exists = db.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team_exists is None:
        abort(404)

    name = request.form.get("name", "").strip()
    role = normalize_player_role(request.form.get("role", ""))
    is_sub = 1 if request.form.get("is_sub") == "1" else 0
    notes = request.form.get("notes", "").strip()

    if not name:
        flash("Player name is required.", "error")
        return redirect(url_for("team_detail", team_id=team_id) + "#roster")

    try:
        db.execute(
            """
            INSERT INTO players (team_id, name, role, is_sub, main_hero, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (team_id, name, role, is_sub, "", notes),
        )
        db.commit()
    except sqlite3.IntegrityError:
        existing = db.execute(
            "SELECT id FROM players WHERE team_id = ? AND lower(name) = lower(?)",
            (team_id, name),
        ).fetchone()
        if existing is None:
            flash("A player with that name already exists on this team.", "error")
            return redirect(url_for("team_detail", team_id=team_id) + "#roster")
        db.execute(
            """
            UPDATE players
            SET role = ?, is_sub = ?, notes = ?
            WHERE id = ?
            """,
            (role, is_sub, notes, existing["id"]),
        )
        db.commit()
        flash("Player already existed, so their details were updated.", "success")
        return redirect(url_for("team_detail", team_id=team_id) + "#roster")

    flash("Player added.", "success")
    return redirect(url_for("team_detail", team_id=team_id) + "#roster")


@app.route("/players/<int:player_id>/edit", methods=["POST"])
def edit_player(player_id: int):
    db = get_db()
    row = db.execute("SELECT team_id FROM players WHERE id = ?", (player_id,)).fetchone()
    if row is None:
        abort(404)

    name = request.form.get("name", "").strip()
    role = normalize_player_role(request.form.get("role", ""))
    is_sub = 1 if request.form.get("is_sub") == "1" else 0
    notes = request.form.get("notes", "").strip()

    if not name:
        flash("Player name is required.", "error")
        return redirect(url_for("team_detail", team_id=row["team_id"]) + "#roster")

    try:
        db.execute(
            """
            UPDATE players
            SET name = ?, role = ?, is_sub = ?, notes = ?
            WHERE id = ?
            """,
            (name, role, is_sub, notes, player_id),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash("A player with that name already exists on this team.", "error")
        return redirect(url_for("team_detail", team_id=row["team_id"]) + "#roster")

    flash("Player updated.", "success")
    return redirect(url_for("team_detail", team_id=row["team_id"]) + "#roster")


@app.route("/players/<int:player_id>/delete", methods=["POST"])
def delete_player(player_id: int):
    db = get_db()
    row = db.execute("SELECT team_id FROM players WHERE id = ?", (player_id,)).fetchone()
    if row is None:
        abort(404)

    db.execute("DELETE FROM players WHERE id = ?", (player_id,))
    db.commit()
    flash("Player removed.", "success")
    return redirect(url_for("team_detail", team_id=row["team_id"]) + "#roster")


@app.route("/teams/<int:team_id>/delete", methods=["POST"])
def delete_team(team_id: int):
    db = get_db()
    deleted = db.execute("DELETE FROM teams WHERE id = ?", (team_id,)).rowcount
    db.commit()
    if not deleted:
        abort(404)
    flash("Team deleted.", "success")
    return redirect(url_for("teams"))


# Team creation (legacy enemy-team route now creates a regular team)
@app.route("/teams/<int:team_id>/enemies/create", methods=["POST"])
def create_enemy_team(team_id: int):
    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    logo_path = save_team_logo(request.files.get("logo"), name)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if not name:
        msg = "Team name is required."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("teams"))

    db = get_db()
    try:
        db.execute(
            "INSERT INTO teams (name, notes, logo_path, is_personal) VALUES (?, ?, ?, 0)",
            (name, notes, logo_path),
        )
        db.commit()
        new_team = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (name,)).fetchone()
        if is_ajax:
            return jsonify({"success": f"Team '{name}' created.", "team_id": new_team["id"] if new_team else None}), 200
        flash(f"Team '{name}' created.", "success")
        if new_team:
            return redirect(url_for("team_detail", team_id=new_team["id"]))
    except sqlite3.IntegrityError:
        existing = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (name,)).fetchone()
        msg = "A team with this name already exists."
        if is_ajax:
            return jsonify({"error": msg, "team_id": existing["id"] if existing else None}), 400
        flash(msg, "error")
        if existing:
            return redirect(url_for("team_detail", team_id=existing["id"]))
    return redirect(url_for("teams"))


@app.route("/enemies/<int:enemy_team_id>/edit", methods=["POST"])
def edit_enemy_team(enemy_team_id: int):
    db = get_db()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    enemy_row = db.execute("SELECT name FROM enemy_teams WHERE id = ?", (enemy_team_id,)).fetchone()
    if not enemy_row:
        if is_ajax:
            return jsonify({"error": "Team not found."}), 404
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    migrate_enemy_teams_to_team_database(db)
    team_row = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (enemy_row["name"],)).fetchone()
    if not team_row:
        if is_ajax:
            return jsonify({"error": "Team not found."}), 404
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    remove_logo = request.form.get("remove_logo", "").strip() == "1"
    new_logo_path = save_team_logo(request.files.get("logo"), name)

    if not name:
        msg = "Team name is required."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("team_detail", team_id=team_row["id"]))

    try:
        current = db.execute("SELECT logo_path FROM teams WHERE id = ?", (team_row["id"],)).fetchone()
        logo_path = current["logo_path"] if current else ""
        if new_logo_path:
            if logo_path and logo_path != new_logo_path:
                delete_team_logo_file(logo_path)
            logo_path = new_logo_path
        elif remove_logo and logo_path:
            delete_team_logo_file(logo_path)
            logo_path = ""
        db.execute(
            "UPDATE teams SET name = ?, notes = ?, logo_path = ? WHERE id = ?",
            (name, notes, logo_path, team_row["id"]),
        )
        db.commit()
        if is_ajax:
            return jsonify({"success": "Team updated."}), 200
        flash("Team updated.", "success")
    except sqlite3.IntegrityError:
        msg = "A team with that name already exists."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")

    if not is_ajax:
        return redirect(url_for("team_detail", team_id=team_row["id"]))
    return jsonify({"success": "Team updated."}), 200


@app.route("/enemies/<int:enemy_team_id>/delete", methods=["POST"])
def delete_enemy_team(enemy_team_id: int):
    db = get_db()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    enemy_row = db.execute("SELECT name FROM enemy_teams WHERE id = ?", (enemy_team_id,)).fetchone()
    if not enemy_row:
        if is_ajax:
            return jsonify({"success": "Team already removed."}), 200
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    migrate_enemy_teams_to_team_database(db)
    team_row = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (enemy_row["name"],)).fetchone()
    if team_row:
        db.execute("DELETE FROM teams WHERE id = ?", (team_row["id"],))
        db.commit()

    if is_ajax:
        return jsonify({"success": "Team removed."}), 200
    flash("Team deleted.", "success")
    return redirect(url_for("teams"))


@app.route("/enemies/<int:enemy_team_id>/players/create", methods=["POST"])
def create_enemy_player(enemy_team_id: int):
    db = get_db()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    enemy_row = db.execute("SELECT name FROM enemy_teams WHERE id = ?", (enemy_team_id,)).fetchone()
    if not enemy_row:
        if is_ajax:
            return jsonify({"error": "Team not found."}), 404
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    migrate_enemy_teams_to_team_database(db)
    team_row = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (enemy_row["name"],)).fetchone()
    if not team_row:
        if is_ajax:
            return jsonify({"error": "Team not found."}), 404
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    name = request.form.get("name", "").strip()
    role = request.form.get("role", "").strip()
    main_hero = request.form.get("main_hero", "").strip()
    notes = request.form.get("notes", "").strip()

    if not name:
        msg = "Player name is required."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("team_detail", team_id=team_row["id"]) + "#roster")

    try:
        db.execute(
            "INSERT INTO players (team_id, name, role, main_hero, notes) VALUES (?, ?, ?, ?, ?)",
            (team_row["id"], name, role, main_hero, notes),
        )
        db.commit()
        if is_ajax:
            return jsonify({"success": f"Player '{name}' added."}), 200
        flash(f"Player '{name}' added.", "success")
    except sqlite3.IntegrityError:
        msg = "A player with that name already exists on this team."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")

    if not is_ajax:
        return redirect(url_for("team_detail", team_id=team_row["id"]) + "#roster")
    return jsonify({"success": "Player added."}), 200


@app.route("/enemy-players/<int:enemy_player_id>/edit", methods=["POST"])
def edit_enemy_player(enemy_player_id: int):
    db = get_db()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    ep_row = db.execute("SELECT enemy_team_id, name FROM enemy_players WHERE id = ?", (enemy_player_id,)).fetchone()
    if not ep_row:
        if is_ajax:
            return jsonify({"error": "Player not found."}), 404
        flash("Player not found.", "info")
        return redirect(url_for("teams"))

    et_row = db.execute("SELECT name FROM enemy_teams WHERE id = ?", (ep_row["enemy_team_id"],)).fetchone()
    if not et_row:
        if is_ajax:
            return jsonify({"error": "Team not found."}), 404
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    migrate_enemy_teams_to_team_database(db)
    team_row = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (et_row["name"],)).fetchone()
    if not team_row:
        if is_ajax:
            return jsonify({"error": "Team not found."}), 404
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    player_row = db.execute(
        "SELECT id FROM players WHERE team_id = ? AND lower(name) = lower(?)",
        (team_row["id"], ep_row["name"]),
    ).fetchone()
    if not player_row:
        if is_ajax:
            return jsonify({"error": "Player not found."}), 404
        flash("Player not found.", "info")
        return redirect(url_for("team_detail", team_id=team_row["id"]) + "#roster")

    name = request.form.get("name", "").strip()
    role = request.form.get("role", "").strip()
    main_hero = request.form.get("main_hero", "").strip()
    notes = request.form.get("notes", "").strip()

    if not name:
        msg = "Player name is required."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("team_detail", team_id=team_row["id"]) + "#roster")

    try:
        db.execute(
            "UPDATE players SET name = ?, role = ?, main_hero = ?, notes = ? WHERE id = ?",
            (name, role, main_hero, notes, player_row["id"]),
        )
        db.commit()
        if is_ajax:
            return jsonify({"success": "Player updated."}), 200
        flash("Player updated.", "success")
    except sqlite3.IntegrityError:
        msg = "A player with that name already exists on this team."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")

    if not is_ajax:
        return redirect(url_for("team_detail", team_id=team_row["id"]) + "#roster")
    return jsonify({"success": "Player updated."}), 200


@app.route("/enemy-players/<int:enemy_player_id>/delete", methods=["POST"])
def delete_enemy_player(enemy_player_id: int):
    db = get_db()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    ep_row = db.execute("SELECT enemy_team_id, name FROM enemy_players WHERE id = ?", (enemy_player_id,)).fetchone()
    if not ep_row:
        if is_ajax:
            return jsonify({"success": "Player already removed."}), 200
        flash("Player not found.", "info")
        return redirect(url_for("teams"))

    et_row = db.execute("SELECT name FROM enemy_teams WHERE id = ?", (ep_row["enemy_team_id"],)).fetchone()
    if not et_row:
        if is_ajax:
            return jsonify({"success": "Team not found."}), 200
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    migrate_enemy_teams_to_team_database(db)
    team_row = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (et_row["name"],)).fetchone()
    if team_row:
        db.execute(
            "DELETE FROM players WHERE team_id = ? AND lower(name) = lower(?)",
            (team_row["id"], ep_row["name"]),
        )
        db.commit()

    if is_ajax:
        return jsonify({"success": "Player removed."}), 200
    flash("Player removed.", "success")
    if team_row:
        return redirect(url_for("team_detail", team_id=team_row["id"]) + "#roster")
    return redirect(url_for("teams"))


@app.route("/enemies/<int:enemy_team_id>")
def enemy_team_detail(enemy_team_id: int):
    """Legacy route: redirect to the unified team_detail page."""
    db = get_db()
    enemy_row = db.execute("SELECT name FROM enemy_teams WHERE id = ?", (enemy_team_id,)).fetchone()
    if not enemy_row:
        flash("This team is already in the Teams database.", "info")
        return redirect(url_for("teams"))
    migrate_enemy_teams_to_team_database(db)
    db.commit()
    team_row = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (enemy_row["name"],)).fetchone()
    if team_row:
        return redirect(url_for("team_detail", team_id=team_row["id"]))
    return redirect(url_for("teams"))


@app.route("/enemies/<int:enemy_team_id>/draft-predict")
def enemy_draft_predict(enemy_team_id: int):
    """Legacy route: return empty predictor data (team is now in main teams table)."""
    return jsonify(build_draft_predictor([], {}))

@app.route("/db/restore", methods=["GET"])
def db_restore_page():
    return render_template("db_restore.html")


@app.route("/scrims")
def scrims():
    season_options = get_scrim_season_options(SCRIMS)
    default_season = get_current_season_from_recent_scrim(SCRIMS)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in SCRIMS)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )
    selected_team_id = request.args.get("team_id", "").strip()
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()

    filtered = filter_scrims_by_season(SCRIMS, selected_season)
    if selected_team_id:
        try:
            tid = int(selected_team_id)
            filtered = [
                s for s in filtered
                if s.get("team_id") == tid
                or s.get("team1_id") == tid
                or s.get("team2_id") == tid
            ]
        except (ValueError, TypeError):
            selected_team_id = ""

    return render_template(
        "scrims.html",
        scrims=list(reversed(filtered)),
        teams=teams,
        map_modes=MAP_MODES,
        today=date.today().isoformat(),
        season_options=season_options,
        selected_season=selected_season,
        has_unseasoned_scrims=has_unseasoned_scrims,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
        selected_team_id=selected_team_id,
        total_scrim_count=len(SCRIMS),
    )


@app.route("/scrims/recover/latest", methods=["POST"])
def recover_latest_scrim_backup():
    global SCRIMS, NEXT_SCRIM_ID, NEXT_MAP_ID, NEXT_EVENT_ID

    db = get_db()
    row = db.execute(
        "SELECT id, created_at, scrims_json FROM app_state_backups ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        flash("No scrim backup is available yet.", "error")
        return redirect(url_for("scrims"))

    try:
        recovered_scrims = json.loads(row["scrims_json"] or "[]")
    except json.JSONDecodeError:
        flash("Backup data was invalid and could not be restored.", "error")
        return redirect(url_for("scrims"))

    if not isinstance(recovered_scrims, list):
        flash("Backup data format is invalid.", "error")
        return redirect(url_for("scrims"))

    current_by_id = {}
    current_no_id = []
    for scrim in SCRIMS:
        if not isinstance(scrim, dict):
            continue
        normalize_scrim_record(scrim)
        scrim_id = scrim.get("id")
        if isinstance(scrim_id, int):
            current_by_id[scrim_id] = scrim
        else:
            current_no_id.append(scrim)

    recovered_no_id = []
    restored_count = 0
    for scrim in recovered_scrims:
        if not isinstance(scrim, dict):
            continue
        normalize_scrim_record(scrim)
        scrim_id = scrim.get("id")
        if isinstance(scrim_id, int):
            if scrim_id not in current_by_id:
                current_by_id[scrim_id] = scrim
                restored_count += 1
        else:
            recovered_no_id.append(scrim)

    merged_scrims = [current_by_id[scrim_id] for scrim_id in sorted(current_by_id.keys())]
    merged_scrims.extend(current_no_id)
    merged_scrims.extend(recovered_no_id)

    max_scrim_id = 0
    max_map_id = 0
    max_event_id = 0
    for scrim in merged_scrims:
        if not isinstance(scrim, dict):
            continue
        if isinstance(scrim.get("id"), int):
            max_scrim_id = max(max_scrim_id, scrim["id"])
        for map_entry in scrim.get("maps", []):
            if not isinstance(map_entry, dict):
                continue
            if isinstance(map_entry.get("id"), int):
                max_map_id = max(max_map_id, map_entry["id"])
            for event in map_entry.get("events", []):
                if isinstance(event, dict) and isinstance(event.get("id"), int):
                    max_event_id = max(max_event_id, event["id"])

    SCRIMS = merged_scrims
    NEXT_SCRIM_ID = max(1, max_scrim_id + 1)
    NEXT_MAP_ID = max(1, max_map_id + 1)
    NEXT_EVENT_ID = max(1, max_event_id + 1)
    save_app_state()

    if restored_count:
        flash(
            f"Recovered {restored_count} missing scrim(s) from backup #{row['id']} ({row['created_at']}).",
            "success",
        )
    else:
        flash("No missing scrims were found in the latest backup.", "warning")
    return redirect(url_for("scrims"))


@app.route("/scrims/clear-duplicates", methods=["POST"])
def clear_scrim_duplicates():
    removed, merged = _dedupe_existing_scrims()
    if removed:
        save_app_state(allow_scrim_removal=True)
        flash(f"Cleared {removed} duplicate scrim{'s' if removed != 1 else ''} ({merged} merged update{'s' if merged != 1 else ''}).", "success")
    else:
        flash("No duplicate scrims were found.", "info")
    return redirect(url_for("scrims"))

@app.route("/db/manual-save", methods=["POST"])
def manual_db_save():
    next_url = (request.form.get("next") or "").strip()
    if not next_url.startswith("/"):
        next_url = url_for("teams")

    try:
        save_app_state()
        backup_path = create_manual_db_backup()
        flash(f"Manual save complete. DB path: {DB_PATH}. Backup written to {backup_path}.", "success")
        if not is_persistent_db_configured():
            flash("Warning: no persistent storage is configured. Data may reset on redeploy.", "warning")
    except Exception as exc:
        flash(f"Manual save failed: {exc}", "error")

    return redirect(next_url)


@app.route("/db/dump-json", methods=["POST"])
def db_dump_json():
    next_url = (request.form.get("next") or "").strip()
    if not next_url.startswith("/"):
        next_url = url_for("teams")

    try:
        save_app_state()
        dump_path = create_manual_json_dump()
        flash(f"Database dump complete. JSON written to {dump_path}.", "success")
        if (os.environ.get("RENDER") or "").strip().lower() == "true" and not is_persistent_db_configured():
            flash("Warning: persistent disk env vars are not configured on Render. Data may reset on redeploy.", "warning")
    except Exception as exc:
        flash(f"Database dump failed: {exc}", "error")

    return redirect(next_url)


@app.route("/db/restore-json", methods=["POST"])
def db_restore_json():
    next_url = (request.form.get("next") or "").strip()
    if not next_url.startswith("/"):
        next_url = url_for("teams")

    try:
        # Get the uploaded file
        if "dump_file" not in request.files:
            flash("No file selected for restore.", "error")
            return redirect(next_url)
        
        file = request.files["dump_file"]
        if file.filename == "":
            flash("No file selected for restore.", "error")
            return redirect(next_url)
        
        # Read and parse JSON
        dump_content = file.read().decode("utf-8")
        dump_data = json.loads(dump_content)
        
        if "data" not in dump_data:
            flash("Invalid dump file format (missing 'data' key).", "error")
            return redirect(next_url)
        
        # Restore data to database
        conn = _connect_db()
        data = dump_data["data"]
        
        try:
            # Clear existing data
            for table_name in ["app_state", "app_state_backups", "team_saved_drafts", "enemy_players", "enemy_teams", "players", "teams"]:
                try:
                    conn.execute(f"DELETE FROM {table_name}")
                except:
                    pass  # Table might not exist
            
            # Insert data from dump
            for table_name, rows in data.items():
                if not rows:
                    continue
                
                # Skip backup history — not needed for restore and very large
                if table_name == "app_state_backups":
                    continue
                
                if table_name == "teams":
                    for row in rows:
                        # Handle is_enemy from old format
                        is_enemy = row.pop("is_enemy", False)
                        conn.execute(
                            """
                            INSERT INTO teams (id, name, notes, logo_path, is_personal, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (row.get("id"), row.get("name"), row.get("notes", ""), 
                             row.get("logo_path", ""), row.get("is_personal", 0), 
                             row.get("created_at", datetime.utcnow().isoformat()))
                        )
                elif table_name == "players":
                    for row in rows:
                        is_enemy = row.pop("is_enemy", False)
                        conn.execute(
                            """
                            INSERT INTO players (id, team_id, name, role, is_sub, main_hero, notes, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (row.get("id"), row.get("team_id"), row.get("name"), row.get("role", ""),
                             row.get("is_sub", 0), row.get("main_hero", ""), row.get("notes", ""),
                             row.get("created_at", datetime.utcnow().isoformat()))
                        )
                elif table_name == "enemy_teams":
                    for row in rows:
                        conn.execute(
                            """
                            INSERT INTO enemy_teams (id, team_id, name, notes, created_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (row.get("id"), row.get("team_id"), row.get("name"), row.get("notes", ""),
                             row.get("created_at", datetime.utcnow().isoformat()))
                        )
                elif table_name == "enemy_players":
                    for row in rows:
                        conn.execute(
                            """
                            INSERT INTO enemy_players (id, enemy_team_id, name, role, is_sub, main_hero, notes, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (row.get("id"), row.get("enemy_team_id"), row.get("name"), row.get("role", ""),
                             row.get("is_sub", 0), row.get("main_hero", ""), row.get("notes", ""),
                             row.get("created_at", datetime.utcnow().isoformat()))
                        )
                elif table_name == "app_state":
                    for row in rows:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO app_state (state_key, state_value)
                            VALUES (?, ?)
                            """,
                            (row.get("state_key"), row.get("state_value"))
                        )
                elif table_name == "team_saved_drafts":
                    for row in rows:
                        conn.execute(
                            """
                            INSERT INTO team_saved_drafts (id, team_id, draft_name, season, draft_slots_json, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (row.get("id"), row.get("team_id"), row.get("draft_name"), row.get("season", ""),
                             row.get("draft_slots_json"), row.get("created_at"))
                        )
            
            conn.commit()
            load_app_state()  # Reload state from database
            flash(f"Database restore complete! Imported data from {file.filename}.", "success")
        finally:
            conn.close()
    
    except json.JSONDecodeError as e:
        flash(f"Invalid JSON file: {e}", "error")
    except Exception as exc:
        flash(f"Database restore failed: {exc}", "error")

    return redirect(next_url)


@app.route("/debug/storage")
def debug_storage():
    return jsonify(
        {
            "db_path": str(DB_PATH),
            "db_exists": DB_PATH.exists(),
            "db_parent": str(DB_PATH.parent),
            "db_parent_writable": os.access(DB_PATH.parent, os.W_OK),
            "on_render": (os.environ.get("RENDER") or "").strip().lower() == "true",
            "database_path_env": bool((os.environ.get("DATABASE_PATH") or "").strip()),
            "render_disk_mount_env": (os.environ.get("RENDER_DISK_MOUNT_PATH") or "").strip(),
            "persistent_configured": is_persistent_db_configured(),
        }
    )


# ── CSV column indices ──────────────────────────────────────────────────────
_CSV_DATE      = 0
_CSV_ENEMY     = 1
_CSV_MAP       = 2
_CSV_SCORE_US  = 3
_CSV_SCORE_THM = 4
_CSV_RESULT    = 5
_CSV_BAN_US1   = 6
_CSV_BAN_TH1   = 7
_CSV_BAN_US2   = 8
_CSV_BAN_TH2   = 9
_CSV_BAN_US3   = 10
_CSV_BAN_TH3   = 11
_CSV_BAN_US4   = 12
_CSV_BAN_TH4   = 13
_CSV_PROT_US1  = 14
_CSV_PROT_TH1  = 15
_CSV_PROT_US2  = 16
_CSV_PROT_TH2  = 17
_CSV_PROTECT_ORDER = 18
# right-side (comp half)
_CSV_R_DATE    = 20
_CSV_R_ENEMY   = 21
_CSV_R_MAP     = 22
_CSV_R_US_RES  = 23
_CSV_R_US_H    = slice(24, 30)   # Tank,Tank,DPS,DPS,Supp,Supp (us)
_CSV_R_TH_RES  = 31
_CSV_R_TH_H    = slice(32, 38)   # Tank,Tank,DPS,DPS,Supp,Supp (them)
_CSV_MIN_COLS  = 38

_TEMPLATE_CSV_REPLAY_CODE = 1
_TEMPLATE_CSV_MAP_TYPE = 2
_TEMPLATE_CSV_TEAM1 = 3
_TEMPLATE_CSV_TEAM2 = 4
_TEMPLATE_CSV_MAP = 5
_TEMPLATE_CSV_DATE = 6
_TEMPLATE_CSV_SEASON = 7
_TEMPLATE_CSV_TEAM1_BAN1 = 8
_TEMPLATE_CSV_TEAM1_BAN2 = 9
_TEMPLATE_CSV_TEAM1_SAVE1 = 10
_TEMPLATE_CSV_TEAM1_BAN3 = 11
_TEMPLATE_CSV_TEAM1_SAVE2 = 12
_TEMPLATE_CSV_TEAM1_BAN4 = 13
_TEMPLATE_CSV_TEAM2_BAN1 = 14
_TEMPLATE_CSV_TEAM2_SAVE1 = 15
_TEMPLATE_CSV_TEAM2_BAN2 = 16
_TEMPLATE_CSV_TEAM2_BAN3 = 17
_TEMPLATE_CSV_TEAM2_BAN4 = 18
_TEMPLATE_CSV_TEAM2_SAVE2 = 19
_TEMPLATE_CSV_TEAM1_PLAYERS = ((20, 21), (22, 23), (24, 25), (26, 27), (28, 29), (30, 31))
_TEMPLATE_CSV_TEAM2_PLAYERS = ((32, 33), (34, 35), (36, 37), (38, 39), (40, 41), (42, 43))
_TEMPLATE_CSV_RESULT = 44
_TEMPLATE_CSV_SCORE_TEAM1 = 45
_TEMPLATE_CSV_SCORE_TEAM2 = 46
_TEMPLATE_CSV_NOTE = 47
_TEMPLATE_CSV_MIN_COLS = 48

# Build a lookup: lowercase submap name → parent map name (e.g. "frozen airfield" → "Hell's Haven")
_SUBMAP_PARENT: dict[str, str] = {}
for _parent, _subs in MAP_SUBMAPS.items():
    for _s in _subs:
        _SUBMAP_PARENT[_s.lower()] = _parent


def _strip_bracket_hint(name: str) -> str:
    """Remove trailing parenthetical abbreviation hints like '(FA, SSF, EM)'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()


def _team_name_match_keys(raw_value: str | None) -> set[str]:
    normalized = (raw_value or "").strip().lower()
    compact = _compact_text(normalized)
    if not compact:
        return set()

    keys = {compact}
    alias_groups = [
        {"100t", "100thieves"},
        {"swamp", "swampgaming"},
    ]
    for group in alias_groups:
        if compact in group:
            keys.update(group)

    tokens = re.findall(r"[a-z0-9]+", normalized)
    filtered_tokens = [
        token for token in tokens
        if token not in {"gaming", "esports", "esport", "team", "club"}
    ]
    if filtered_tokens:
        keys.add("".join(filtered_tokens))

    return {key for key in keys if key}


def _team_names_match(left: str | None, right: str | None) -> bool:
    left_keys = _team_name_match_keys(left)
    right_keys = _team_name_match_keys(right)
    return bool(left_keys and right_keys and left_keys.intersection(right_keys))


def normalize_map_type_value(raw_value: str | None) -> str:
    normalized = _compact_text(raw_value or "")
    return MAP_TYPE_ALIASES.get(normalized, DEFAULT_MAP_TYPE)


def _match_map_name(raw: str) -> str:
    """
    Try to find the closest canonical map name from MAPS for a raw string.
    Falls back to the raw string stripped of bracket hints if no match found.
    """
    base = _strip_bracket_hint(raw)
    base_lower = base.lower()
    compact = _compact_text(base)

    alias_lookup = {
        "hellsheaven": "Hell's Haven",
        "hellshaven": "Hell's Haven",
        "birnintchalla": "Birin T'Challa",
        "birintchalla": "Birin T'Challa",
        "celestialhusk": "Celestial",
    }
    aliased = alias_lookup.get(compact)
    if aliased:
        return aliased

    # Exact match
    for m in MAPS:
        if m.lower() == base_lower:
            return m
    compact_map = {_compact_text(m): m for m in MAPS}
    if compact in compact_map:
        return compact_map[compact]
    # Prefix match: raw starts with canonical map name
    for m in sorted(MAPS, key=len, reverse=True):
        if base_lower.startswith(m.lower()):
            return m
        compact_name = _compact_text(m)
        if compact and compact_name and compact.startswith(compact_name):
            return m

    best_match = None
    best_score = 0.0
    for m in MAPS:
        score = SequenceMatcher(None, compact, _compact_text(m)).ratio()
        if score > best_score:
            best_score = score
            best_match = m
    if best_match and best_score >= 0.74:
        return best_match
    return base


def _compact_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def normalize_hero_slot_value(raw_hero: str | None) -> str:
    hero_text = (raw_hero or "").strip()
    if not hero_text:
        return ""

    resolved = _resolve_hero_transform_key(hero_text)
    return resolved or hero_text


def _resolve_hero_transform_key(hero_name: str) -> str | None:
    """Return the best HERO_TRANSFORMATIONS key for a potentially misspelled hero name."""
    raw = (hero_name or "").strip()
    if not raw:
        return None

    if raw in HERO_TRANSFORMATIONS:
        return raw

    compact = _compact_text(raw)
    if not compact:
        return None

    mapped = HERO_NAME_ALIASES.get(compact)
    if mapped:
        return mapped

    if compact.startswith("deadpool"):
        if any(token in compact for token in ("tank", "vanguard")):
            return "Tankpool"
        if any(token in compact for token in ("support", "strategist", "supp")):
            return "SupportPool"
        if "dps" in compact or "duelist" in compact:
            return "DpsPool"

    compact_map = { _compact_text(k): k for k in HERO_TRANSFORMATIONS.keys() }
    if compact in compact_map:
        return compact_map[compact]

    best_key = None
    best_score = 0.0
    for key in HERO_TRANSFORMATIONS.keys():
        score = SequenceMatcher(None, compact, _compact_text(key)).ratio()
        if score > best_score:
            best_score = score
            best_key = key

    if best_key and best_score >= 0.78:
        return best_key
    return None


_POOL_HERO_ROLE_ICONS: dict[str, str] = {
    "Tankpool": "/static/role-icons/Vanguard.webp",
    "DpsPool": "/static/role-icons/Duelist.webp",
    "SupportPool": "/static/role-icons/Strategist.webp",
}


def _hero_image_url(hero_name: str) -> str:
    safe_name = (hero_name or "Hero").strip() or "Hero"
    return f"/hero-image/{quote(safe_name[:80], safe='')}"


HERO_IMAGE_CACHE_TTL_SECONDS = 60 * 60 * 24
_HERO_IMAGE_CACHE: dict[str, tuple[float, bytes, str]] = {}


def _hero_image_candidate_urls(hero_name: str) -> list[str]:
    transform_key = _resolve_hero_transform_key(hero_name)
    candidates: list[str] = []

    if transform_key:
        images = HERO_TRANSFORMATIONS.get(transform_key) or []
        if images:
            filename = Path(images[0]).name
            dotgg_filename = re.sub(r"-\d+(?=\.webp$)", "", filename)
            candidates.append(f"https://static.dotgg.gg/rivals/characters/{dotgg_filename}")

    return candidates


def _hero_image_placeholder_svg(hero_name: str) -> str:
    text = (hero_name or "Hero").strip()[:24] or "Hero"
    return (
        "<svg xmlns='http://www.w3.org/2000/svg' width='80' height='80' viewBox='0 0 80 80'>"
        "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        "<stop offset='0%' stop-color='#111827'/><stop offset='100%' stop-color='#1f2937'/></linearGradient></defs>"
        "<rect width='80' height='80' fill='url(#g)' rx='10'/>"
        f"<text x='40' y='44' text-anchor='middle' font-size='11' font-family='Arial, sans-serif' fill='#e6edf3'>{text}</text>"
        "</svg>"
    )


@app.route("/hero-image/<path:hero_name>")
def hero_image_proxy(hero_name: str):
    requested = (hero_name or "").strip() or "Hero"
    cache_key = _resolve_hero_transform_key(requested) or requested
    now = time.time()
    cached = _HERO_IMAGE_CACHE.get(cache_key)
    if cached and (now - cached[0]) < HERO_IMAGE_CACHE_TTL_SECONDS:
        return Response(
            cached[1],
            mimetype=cached[2],
            headers={"Cache-Control": "public, max-age=86400"},
        )

    for image_url in _hero_image_candidate_urls(requested):
        try:
            remote_request = Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(remote_request, timeout=4) as remote_response:
                content_type = remote_response.headers.get_content_type() or "image/webp"
                if not content_type.startswith("image/"):
                    continue
                payload = remote_response.read()
                if not payload:
                    continue
                _HERO_IMAGE_CACHE[cache_key] = (now, payload, content_type)
                return Response(
                    payload,
                    mimetype=content_type,
                    headers={"Cache-Control": "public, max-age=86400"},
                )
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            continue

    placeholder = _hero_image_placeholder_svg(requested).encode("utf-8")
    return Response(
        placeholder,
        mimetype="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _hero_pool_label(hero_name: str) -> str:
    canonical = _resolve_hero_transform_key(hero_name) or (hero_name or "").strip()
    if canonical == "Tankpool":
        return "tank"
    if canonical == "DpsPool":
        return "dps"
    if canonical == "SupportPool":
        return "supp"
    return ""


def _hero_display_name(hero_name: str) -> str:
    canonical = _resolve_hero_transform_key(hero_name) or (hero_name or "").strip()
    if canonical in ("Tankpool", "DpsPool", "SupportPool"):
        return "Deadpool"
    return canonical or (hero_name or "")


@app.context_processor
def inject_template_helpers():
    def _sample_warn(n, threshold: int = 5) -> Markup:
        """Return the count with a ⚠ icon when below the sample threshold."""
        try:
            count = int(n)
        except (TypeError, ValueError):
            return Markup(str(n) if n is not None else "")
        if count < threshold:
            return Markup(
                f'{count}\u202f<span class="sample-warn-icon" '
                f'title="Low sample size (fewer than {threshold} maps)">⚠</span>'
            )
        return Markup(str(count))

    def _team_logo_url(logo_path: str) -> str:
        """Return the URL to display for a team logo_path value."""
        if not logo_path:
            return ""
        if logo_path.startswith("__disk__/"):
            filename = logo_path[len("__disk__/"):]
            from flask import url_for as _url_for
            return _url_for("serve_team_logo", filename=filename)
        from flask import url_for as _url_for
        return _url_for("static", filename=logo_path)

    return {
        "hero_image_url": _hero_image_url,
        "hero_pool_label": _hero_pool_label,
        "hero_display_name": _hero_display_name,
        "sample_warn": _sample_warn,
        "pool_role_icons": _POOL_HERO_ROLE_ICONS,
        "team_logo_url": _team_logo_url,
    }


def _canonicalize_submap_name(parent_map: str, raw_submap: str) -> str:
    """Normalize imported submap text to a canonical submap for the parent map."""
    clean = (raw_submap or "").strip()
    if not clean:
        return ""

    candidates = MAP_SUBMAPS.get(parent_map, [])
    if not candidates:
        return clean

    clean_compact = _compact_text(clean)
    parent_compact = _compact_text(parent_map)

    # Some sheets repeat the parent map in the submap value.
    if parent_compact and clean_compact.startswith(parent_compact):
        clean_compact = clean_compact[len(parent_compact):]

    # Direct/fuzzy match against known submaps for this map.
    for candidate in candidates:
        candidate_compact = _compact_text(candidate)
        if clean_compact == candidate_compact:
            return candidate
        if clean_compact and (clean_compact in candidate_compact or candidate_compact in clean_compact):
            return candidate

    # Known analyst-sheet abbreviations.
    alias_lookup = {
        "Hell's Haven": {
            "fa": "Frozen Airfield",
            "ssf": "Super-Soldier Factory",
            "em": "Eldritch Monument",
            "supersoilderfactory": "Super-Soldier Factory",
            "hellshavensupersoilderfactory": "Super-Soldier Factory",
        },
        "Birin T'Challa": {
            "iis": "Imperial Institute of Science",
            "impinsscience": "Imperial Institute of Science",
            "ss": "Stellar Spaceport",
            "wf": "Warrior Falls",
        },
        "Krakoa": {
            "ca": "Carousel",
            "gr": "Grove",
            "cr": "Cradle",
        },
        "Celestial": {
            "co": "Codex",
            "va": "Vault",
            "ha": "Hand",
        },
    }

    mapped = alias_lookup.get(parent_map, {}).get(clean_compact)
    if mapped:
        return mapped

    # Fuzzy match for misspellings (e.g. Soilder vs Soldier).
    best_candidate = ""
    best_score = 0.0
    for candidate in candidates:
        candidate_compact = _compact_text(candidate)
        score = SequenceMatcher(None, clean_compact, candidate_compact).ratio()
        if score > best_score:
            best_score = score
            best_candidate = candidate
    if best_candidate and best_score >= 0.72:
        return best_candidate

    return clean


def split_score_pair(raw_score: str) -> tuple[str, str]:
    value = (raw_score or "").strip()
    if not value:
        return "", ""

    matches = re.findall(r"\d+(?:\.\d+)?", value)
    if not matches:
        return "", ""
    if len(matches) == 1:
        return matches[0], ""
    return matches[0], matches[1]


def build_score_text(left_score: str, right_score: str, fallback: str = "") -> str:
    left = (left_score or "").strip()
    right = (right_score or "").strip()
    if left and right:
        return f"{left}-{right}"
    if left:
        return left
    if right:
        return right
    return (fallback or "").strip()


def score_for_perspective(raw_score: str, *, perspective: str = "left") -> float | None:
    left_score, right_score = split_score_pair(raw_score)
    target = left_score if perspective == "left" else right_score
    if not target:
        return None
    try:
        return float(target)
    except ValueError:
        return None


def flip_score_text(raw_score: str) -> str:
    left_score, right_score = split_score_pair(raw_score)
    if left_score and right_score:
        return f"{right_score}-{left_score}"
    return (raw_score or "").strip()


def _our_team_slot_from_protect_order(raw_value: str) -> str:
    """
    Determine our team slot from the CSV 1st/2nd protect column.
    User rule: 2nd protect = team1, so 1st protect = team2.
    """
    value = (raw_value or "").strip().lower()
    if not value:
        return "team1"
    if value.startswith("2") or "second" in value:
        return "team1"
    if value.startswith("1") or "first" in value:
        return "team2"
    return "team1"


def _parse_csv_into_scrims(
    raw_text: str,
    team_id: int | None,
    team_name: str,
) -> tuple[list[dict], list[str]]:
    """
    Parse the analyst CSV format and return (scrim_list, warning_list).

    CSV structure (0-indexed columns):
      0   Date
      1   Enemy team name
      2   Map name (may include submap/side suffix or bracket hint)
      3   Our score
      4   Their score
      5   Result (Won/Lost)
      6   Ban Us1          7  Ban Them1
      8   Ban Us2          9  Ban Them2
      10  Ban Us3          11 Ban Them3
      12  Ban Us4          13 Ban Them4
      14  Protect Us1      15 Protect Them1
      16  Protect Us2      17 Protect Them2
      18  1st/2nd Protect  (ignored)
      19  (separator)
      20  Date (right half – used when left is blank)
      21  Enemy (right half)
      22  Map (right half)
      23  Us Result
      24–29  Our heroes (Tank,Tank,DPS,DPS,Supp,Supp)
      30  (separator)
      31  Them Result
      32–37  Their heroes
    """

    warnings: list[str] = []

    reader = csv.reader(io.StringIO(raw_text))
    rows = list(reader)
    if not rows:
        return [], ["CSV file is empty."]

    # Skip the header row (it starts with empty cells then "Ban Us1" etc.)
    header = rows[0]
    data_rows = rows[1:]

    def _pad(row: list[str]) -> list[str]:
        while len(row) < _CSV_MIN_COLS:
            row.append("")
        return row

    def _cell(row: list[str], idx: int) -> str:
        return row[idx].strip() if idx < len(row) else ""

    def _heroes(row: list[str], sl: slice) -> list[dict]:
        return [{"hero": normalize_hero_slot_value(h), "player": ""} for h in row[sl]]

    # ── group rows into (date, enemy) buckets keeping insertion order ──────
    # We preserve the order that maps appear so that sub-rows stay near parents.
    # Structure: { (date, enemy): [padded_row, ...] }
    from collections import OrderedDict
    buckets: OrderedDict[tuple[str, str], list[list[str]]] = OrderedDict()
    short_row_count = 0

    for raw_row in data_rows:
        if len(raw_row) < _CSV_MIN_COLS:
            short_row_count += 1
            continue

        row = _pad(list(raw_row))
        left_filled  = any(row[i].strip() for i in range(20))
        right_filled = any(row[i].strip() for i in range(20, _CSV_MIN_COLS))
        if not left_filled and not right_filled:
            continue

        if left_filled:
            date_val  = _cell(row, _CSV_DATE)
            enemy_val = _cell(row, _CSV_ENEMY)
        else:
            date_val  = _cell(row, _CSV_R_DATE)
            enemy_val = _cell(row, _CSV_R_ENEMY)

        if not date_val and not enemy_val:
            continue

        key = (date_val, enemy_val)
        buckets.setdefault(key, []).append(row)

    if not buckets:
        if short_row_count:
            return [], [f"Skipped {short_row_count} row(s) with missing columns."]
        return [], ["No data rows found in CSV."]

    # ── build scrim objects from each bucket ──────────────────────────────
    all_scrims: list[dict] = []

    for (scrim_date, enemy_name), bucket_rows in buckets.items():
        maps: list[dict] = []
        current_parent_idx: int | None = None

        for row in bucket_rows:
            left_filled = any(row[i].strip() for i in range(20))

            # Gather map name from whichever side is available
            if left_filled:
                raw_map_name = _cell(row, _CSV_MAP)
            else:
                raw_map_name = _cell(row, _CSV_R_MAP)

            if not raw_map_name:
                continue

            normalized_map_name = raw_map_name.strip()
            normalized_map_name_lower = normalized_map_name.lower()
            is_side_row = normalized_map_name_lower.endswith(" attack") or normalized_map_name_lower.endswith(" defense")

            # Right-side heroes / results
            our_heroes  = _heroes(row, _CSV_R_US_H)
            their_heroes = _heroes(row, _CSV_R_TH_H)
            has_comp = any(h["hero"] for h in our_heroes + their_heroes)

            has_bans = any(
                row[i].strip()
                for i in [_CSV_BAN_US1, _CSV_BAN_TH1, _CSV_BAN_US2, _CSV_BAN_TH2,
                           _CSV_BAN_US3, _CSV_BAN_TH3, _CSV_BAN_US4, _CSV_BAN_TH4,
                           _CSV_PROT_US1, _CSV_PROT_TH1, _CSV_PROT_US2, _CSV_PROT_TH2]
            )

            # Parent map rows are map-level rows. Side split rows like "Midtown Attack"
            # should never become standalone parent maps.
            is_parent = has_bans or (left_filled and not has_comp and not is_side_row)

            if is_parent:
                canonical = _match_map_name(raw_map_name)
                base_name = _strip_bracket_hint(raw_map_name)
                our_team_slot = _our_team_slot_from_protect_order(_cell(row, _CSV_PROTECT_ORDER))
                enemy_team_slot = opposite_team_slot(our_team_slot)

                left_result = _cell(row, _CSV_RESULT)
                result_str  = "Win" if left_result == "Won" else "Loss" if left_result == "Lost" else ""

                score_us  = _cell(row, _CSV_SCORE_US)
                score_thm = _cell(row, _CSV_SCORE_THM)
                score_str = f"{score_us}-{score_thm}" if score_us or score_thm else ""

                draft_our = {
                    "ban1":     _cell(row, _CSV_BAN_US1),
                    "ban2":     _cell(row, _CSV_BAN_US2),
                    "protect1": _cell(row, _CSV_PROT_US1),
                    "ban3":     _cell(row, _CSV_BAN_US3),
                    "protect2": _cell(row, _CSV_PROT_US2),
                    "ban4":     _cell(row, _CSV_BAN_US4),
                }
                draft_enemy = {
                    "ban1":     _cell(row, _CSV_BAN_TH1),
                    "ban2":     _cell(row, _CSV_BAN_TH2),
                    "protect1": _cell(row, _CSV_PROT_TH1),
                    "ban3":     _cell(row, _CSV_BAN_TH3),
                    "protect2": _cell(row, _CSV_PROT_TH2),
                    "ban4":     _cell(row, _CSV_BAN_TH4),
                }
                draft = {
                    our_team_slot: draft_our,
                    enemy_team_slot: draft_enemy,
                }

                map_entry = {
                    "_base_name": base_name,        # temp field for sub-row matching
                    "map_name":   canonical,
                    "side":       "",
                    "our_team_slot": our_team_slot,
                    "result":     result_str,
                    "score":      score_str,
                    "draft":      draft,
                    "comp":       build_default_comp_sections(canonical),
                    "notes":      "",
                    "vod_url":    "",
                    "events":     [],
                }
                maps.append(map_entry)
                current_parent_idx = len(maps) - 1

            elif has_comp or is_side_row:
                # Sub-row: comp section belonging to the preceding parent
                # Determine parent by name prefix match
                parent_idx = current_parent_idx
                if parent_idx is None:
                    # No parent yet. Only create an implicit parent if this row has comp
                    # payload. For empty side rows, skip to avoid inflating map counts.
                    if not has_comp:
                        warnings.append(
                            f"Skipped side row without parent: {raw_map_name} ({scrim_date} vs {enemy_name})."
                        )
                        continue

                    # No parent yet — create an implicit parent
                    canonical = _match_map_name(raw_map_name)
                    map_entry = {
                        "_base_name": canonical,
                        "map_name":   canonical,
                        "side":       "",
                        "our_team_slot": "team1",
                        "result":     "",
                        "score":      "",
                        "draft":      {"team1": {"ban1":"","ban2":"","protect1":"","ban3":"","protect2":"","ban4":""}, "team2": {"ban1":"","ban2":"","protect1":"","ban3":"","protect2":"","ban4":""}},
                        "comp":       build_default_comp_sections(canonical),
                        "notes":      "",
                        "vod_url":    "",
                        "events":     [],
                    }
                    maps.append(map_entry)
                    parent_idx = len(maps) - 1
                    current_parent_idx = parent_idx

                parent = maps[parent_idx]
                base = parent["_base_name"]
                suffix = raw_map_name[len(base):].strip() if raw_map_name.lower().startswith(base.lower()) else raw_map_name

                submap = ""
                section_side = ""
                if suffix in ("Attack", "Defense"):
                    section_side = suffix
                elif suffix:
                    submap = _canonicalize_submap_name(parent.get("map_name", ""), suffix)

                score_us = _cell(row, _CSV_SCORE_US)
                score_thm = _cell(row, _CSV_SCORE_THM)
                section_score = ""
                if score_us and score_thm:
                    section_score = f"{score_us}-{score_thm}"
                elif score_us:
                    section_score = score_us
                elif score_thm:
                    section_score = score_thm

                # Fallback: some analyst sheets only include side outcome (Won/Lost)
                # for Attack/Defense rows. Use 1/0 so averages still populate.
                if not section_score:
                    us_result = _cell(row, _CSV_R_US_RES).lower()
                    if us_result == "won":
                        section_score = "1"
                    elif us_result == "lost":
                        section_score = "0"

                # Pad heroes to 6 slots each
                while len(our_heroes) < 6:
                    our_heroes.append({"hero": "", "player": ""})
                while len(their_heroes) < 6:
                    their_heroes.append({"hero": "", "player": ""})

                parent_our_slot = parent.get("our_team_slot", "team1")
                if parent_our_slot not in TEAM_SLOTS:
                    parent_our_slot = "team1"

                section = {
                    "submap": submap,
                    "side":   section_side,
                    "score":  section_score,
                    "team1":  our_heroes[:6] if parent_our_slot == "team1" else their_heroes[:6],
                    "team2":  our_heroes[:6] if parent_our_slot == "team2" else their_heroes[:6],
                }

                # Assign comps to the matching prebuilt section for this map type
                # (submap for Control, side for Escort/Hybrid). If no match exists,
                # append as a fallback.
                target_index = None
                for idx, existing in enumerate(parent.get("comp", [])):
                    existing_submap = (existing.get("submap") or "").strip().lower()
                    existing_side = (existing.get("side") or "").strip().lower()
                    if section_side:
                        if existing_side == section_side.strip().lower():
                            target_index = idx
                            break
                    elif submap:
                        if _compact_text(existing_submap) == _compact_text(submap):
                            target_index = idx
                            break

                if target_index is None:
                    parent.setdefault("comp", []).append(section)
                else:
                    parent["comp"][target_index].update(
                        {
                            "score": section_score,
                            "team1": our_heroes[:6] if parent_our_slot == "team1" else their_heroes[:6],
                            "team2": our_heroes[:6] if parent_our_slot == "team2" else their_heroes[:6],
                        }
                    )

        # Remove temp field
        for m in maps:
            m.pop("_base_name", None)

        if not maps:
            warnings.append(f"No maps found for scrim {scrim_date} vs {enemy_name}.")
            continue

        scrim = {
            "opponent":      enemy_name,
            "enemy_team":    enemy_name,
            "enemy_team_id": None,
            "scrim_date":    scrim_date,
            "season":       "",
            "team_id":       team_id,
            "team_name":     team_name,
            "notes":         "",
            "maps":          maps,
        }
        all_scrims.append(scrim)

    return all_scrims, warnings


def _parse_template_csv_into_scrims(
    raw_text: str,
    team_id: int | None,
    team_name: str,
) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []

    reader = csv.reader(io.StringIO(raw_text))
    rows = list(reader)
    if not rows:
        return [], ["CSV file is empty."]

    data_rows = rows[1:]
    selected_team_name = (team_name or "").strip()

    from collections import OrderedDict
    buckets: OrderedDict[tuple[str, str], list[list[str]]] = OrderedDict()

    def _pad(row: list[str]) -> list[str]:
        while len(row) < _TEMPLATE_CSV_MIN_COLS:
            row.append("")
        return row

    def _cell(row: list[str], idx: int) -> str:
        return row[idx].strip() if idx < len(row) else ""

    def _build_team_comp(row: list[str], pairs: tuple[tuple[int, int], ...]) -> list[dict]:
        slots: list[dict] = []
        for player_idx, hero_idx in pairs:
            raw_player_name = _cell(row, player_idx)
            slots.append(
                {
                    "player": "" if is_ringer_player_name(raw_player_name) else normalize_player_name(raw_player_name),
                    "hero": normalize_hero_slot_value(_cell(row, hero_idx)),
                }
            )
        return slots

    def _result_for_selected_team(result_value: str, team1_name: str, team2_name: str, our_slot: str) -> str:
        normalized = (result_value or "").strip().lower()
        if not normalized:
            return ""
        if normalized in {"draw", "tie"}:
            return ""
        if normalized in {"win", "won"}:
            return "Win"
        if normalized in {"loss", "lost"}:
            return "Loss"

        team1_normalized = (team1_name or "").strip().lower()
        team2_normalized = (team2_name or "").strip().lower()
        if normalized == team1_normalized:
            return "Win" if our_slot == "team1" else "Loss"
        if normalized == team2_normalized:
            return "Win" if our_slot == "team2" else "Loss"
        return ""

    for raw_row in data_rows:
        if not any(cell.strip() for cell in raw_row):
            continue
        row = _pad(list(raw_row))

        team1_name = _cell(row, _TEMPLATE_CSV_TEAM1)
        team2_name = _cell(row, _TEMPLATE_CSV_TEAM2)
        map_name = _cell(row, _TEMPLATE_CSV_MAP)
        scrim_date = _cell(row, _TEMPLATE_CSV_DATE)

        if not team1_name or not team2_name or not map_name or not scrim_date:
            continue

        if _team_names_match(team1_name, selected_team_name):
            enemy_name = team2_name
        elif _team_names_match(team2_name, selected_team_name):
            enemy_name = team1_name
        else:
            warnings.append(f"Skipped row for {team1_name} vs {team2_name} on {scrim_date}: selected team not found in row.")
            continue

        buckets.setdefault((scrim_date, enemy_name), []).append(row)

    if not buckets:
        return [], warnings or ["No matching rows found for the selected team in CSV."]

    all_scrims: list[dict] = []

    for (scrim_date, enemy_name), bucket_rows in buckets.items():
        maps: list[dict] = []
        for row in bucket_rows:
            team1_name = _cell(row, _TEMPLATE_CSV_TEAM1)
            team2_name = _cell(row, _TEMPLATE_CSV_TEAM2)
            canonical_map = _match_map_name(_cell(row, _TEMPLATE_CSV_MAP))
            our_team_slot = "team1" if _team_names_match(team1_name, selected_team_name) else "team2"

            team1_score = _cell(row, _TEMPLATE_CSV_SCORE_TEAM1)
            team2_score = _cell(row, _TEMPLATE_CSV_SCORE_TEAM2)
            score_text = build_score_text(team1_score, team2_score)
            result_text = infer_result_from_score_text(score_text, slot=our_team_slot)
            if not result_text:
                result_text = _result_for_selected_team(_cell(row, _TEMPLATE_CSV_RESULT), team1_name, team2_name, our_team_slot)

            draft = {
                "team1": {
                    "ban1": _cell(row, _TEMPLATE_CSV_TEAM1_BAN1),
                    "ban2": _cell(row, _TEMPLATE_CSV_TEAM1_BAN2),
                    "protect1": _cell(row, _TEMPLATE_CSV_TEAM1_SAVE1),
                    "ban3": _cell(row, _TEMPLATE_CSV_TEAM1_BAN3),
                    "protect2": _cell(row, _TEMPLATE_CSV_TEAM1_SAVE2),
                    "ban4": _cell(row, _TEMPLATE_CSV_TEAM1_BAN4),
                },
                "team2": {
                    "ban1": _cell(row, _TEMPLATE_CSV_TEAM2_BAN1),
                    "ban2": _cell(row, _TEMPLATE_CSV_TEAM2_BAN2),
                    "protect1": _cell(row, _TEMPLATE_CSV_TEAM2_SAVE1),
                    "ban3": _cell(row, _TEMPLATE_CSV_TEAM2_BAN3),
                    "protect2": _cell(row, _TEMPLATE_CSV_TEAM2_SAVE2),
                    "ban4": _cell(row, _TEMPLATE_CSV_TEAM2_BAN4),
                },
            }

            comp_sections = build_default_comp_sections(canonical_map)
            if comp_sections:
                team1_comp = _build_team_comp(row, _TEMPLATE_CSV_TEAM1_PLAYERS)
                team2_comp = _build_team_comp(row, _TEMPLATE_CSV_TEAM2_PLAYERS)
                for idx, section in enumerate(comp_sections):
                    section["team1"] = copy.deepcopy(team1_comp)
                    section["team2"] = copy.deepcopy(team2_comp)
                    if idx == 0:
                        section["score"] = score_text

            maps.append(
                {
                    "map_name": canonical_map,
                    "map_type": normalize_map_type_value(_cell(row, _TEMPLATE_CSV_MAP_TYPE)),
                    "side": "",
                    "our_team_slot": our_team_slot,
                    "result": result_text,
                    "score": score_text,
                    "draft": draft,
                    "comp": comp_sections,
                    "notes": _cell(row, _TEMPLATE_CSV_NOTE),
                    "vod_url": "",
                    "events": [],
                    "team1_name": team1_name,
                    "team2_name": team2_name,
                }
            )

        if not maps:
            continue

        all_scrims.append(
            {
                "opponent": enemy_name,
                "enemy_team": enemy_name,
                "enemy_team_id": None,
                "scrim_date": scrim_date,
                "season": "",
                "team_id": team_id,
                "team_name": team_name,
                "notes": "",
                "maps": maps,
            }
        )

    return all_scrims, warnings


def parse_csv_into_scrims(
    raw_text: str,
    team_id: int | None,
    team_name: str,
) -> tuple[list[dict], list[str]]:
    reader = csv.reader(io.StringIO(raw_text))
    rows = list(reader)
    if not rows:
        return [], ["CSV file is empty."]

    header = [cell.strip().lower() for cell in rows[0]]
    is_template_csv = (
        len(header) >= _TEMPLATE_CSV_MIN_COLS
        and "replay code" in header
        and "map type" in header
        and "team 1" in header
        and "team 2" in header
        and "map" in header
    )

    if is_template_csv:
        return _parse_template_csv_into_scrims(raw_text, team_id, team_name)
    return _parse_csv_into_scrims(raw_text, team_id, team_name)


def summarize_import_warnings(warnings: list[str], *, preview_count: int = 5) -> str:
    if not warnings:
        return ""
    preview = warnings[:preview_count]
    remaining = len(warnings) - len(preview)
    message = " | ".join(preview)
    if remaining > 0:
        message += f" | {remaining} more warning(s)"
    return message


@app.route("/scrims/import-csv", methods=["POST"])
def import_csv_scrims():
    global NEXT_SCRIM_ID

    team_id  = parse_team_id(request.form.get("team_id", ""))
    team_name = get_team_name_by_id(team_id)
    season = normalize_season_value(request.form.get("season", ""))
    if not team_name:
        flash("Please select your team before importing.", "error")
        return redirect(url_for("scrims"))
    if not season:
        flash("Please set a season for this import.", "error")
        return redirect(url_for("scrims"))

    file = request.files.get("csv_file")
    if not file or not file.filename:
        flash("No CSV file selected.", "error")
        return redirect(url_for("scrims"))

    ext = Path(file.filename).suffix.lower()
    if ext not in {".csv", ".txt"}:
        flash("Only .csv files are supported.", "error")
        return redirect(url_for("scrims"))

    try:
        raw_text = file.read().decode("utf-8-sig")  # handle BOM
    except UnicodeDecodeError:
        try:
            file.seek(0)
            raw_text = file.read().decode("latin-1")
        except Exception:
            flash("Could not decode the CSV file. Make sure it is UTF-8 encoded.", "error")
            return redirect(url_for("scrims"))

    parsed_scrims, warnings = parse_csv_into_scrims(raw_text, team_id, team_name)

    if not parsed_scrims:
        warning_summary = summarize_import_warnings(warnings)
        flash("No scrims could be imported from that CSV. " + warning_summary, "error")
        return redirect(url_for("scrims"))

    # Try to match enemy team names to existing teams in the global team database
    db = get_db()
    migrate_enemy_teams_to_team_database(db)
    enemy_rows = db.execute(
        "SELECT id, name FROM teams WHERE id != ?", (team_id,)
    ).fetchall() if team_id else []
    enemy_lookup: dict[str, int] = {}
    for row in enemy_rows:
        for key in _team_name_match_keys(row["name"]):
            enemy_lookup.setdefault(key, row["id"])

    imported = 0
    updated = 0
    for scrim in parsed_scrims:
        scrim["season"] = season
        normalize_scrim_record(scrim)
        _prepare_imported_scrim_context(scrim, team_id, team_name, enemy_lookup)
        _sync_scrim_rosters_with_database(scrim)

        if scrim.get("enemy_team_id"):
            for enemy_key in _team_name_match_keys(scrim["enemy_team"]):
                enemy_lookup[enemy_key] = scrim["enemy_team_id"]

        existing_scrim = _find_duplicate_scrim_for_import(scrim)
        if existing_scrim is not None:
            _merge_imported_scrim(existing_scrim, scrim)
            _assign_missing_scrim_ids(existing_scrim)
            updated += 1
            continue

        scrim["id"] = NEXT_SCRIM_ID
        NEXT_SCRIM_ID += 1
        _assign_missing_scrim_ids(scrim)
        SCRIMS.append(scrim)
        imported += 1

    save_app_state()

    msg_parts = []
    if imported:
        msg_parts.append(f"Imported {imported} scrim{'s' if imported != 1 else ''}")
    if updated:
        msg_parts.append(f"updated {updated} duplicate{'s' if updated != 1 else ''}")
    msg = ". ".join(msg_parts) + "."
    if warnings:
        msg += " Warnings: " + summarize_import_warnings(warnings)
    flash(msg, "success")
    return redirect(url_for("scrims"))


@app.route("/api/teams/<int:team_id>/enemies")
def api_get_enemy_teams(team_id: int):
    """API endpoint to get opponent teams for a specific team from global teams db."""
    db = get_db()
    migrate_enemy_teams_to_team_database(db)
    team = db.execute("SELECT id FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    enemy_team_rows = db.execute(
        "SELECT id, name FROM teams WHERE id != ? ORDER BY name COLLATE NOCASE",
        (team_id,),
    ).fetchall()

    return jsonify([{
        "id": row["id"],
        "name": row["name"],
    } for row in enemy_team_rows])


@app.route("/api/teams")
def api_get_teams():
    team_rows = get_db().execute(
        "SELECT id, name FROM teams ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return jsonify([
        {
            "id": row["id"],
            "name": row["name"],
        }
        for row in team_rows
    ])


@app.route("/api/teams/<int:team_id>/saved-drafts", methods=["GET", "POST"])
def api_team_saved_drafts(team_id: int):
    db = get_db()
    team = db.execute("SELECT id FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    if request.method == "GET":
        rows = db.execute(
            """
            SELECT id, draft_name, season, draft_slots_json, created_at
            FROM team_saved_drafts
            WHERE team_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (team_id,),
        ).fetchall()
        drafts = []
        for row in rows:
            try:
                stored_payload = json.loads(row["draft_slots_json"] or "{}")
            except json.JSONDecodeError:
                stored_payload = {}

            mode = "full"
            slots = _sanitize_simulator_draft_slots({})
            concept_slots = _sanitize_one_sided_concept_slots({})

            if isinstance(stored_payload, dict) and "mode" in stored_payload:
                mode = (stored_payload.get("mode") or "full").strip() or "full"
                slots = _sanitize_simulator_draft_slots(stored_payload.get("slots"))
                concept_slots = _sanitize_one_sided_concept_slots(stored_payload.get("concept_slots"))
            else:
                # Legacy rows store full draft slots at the root.
                slots = _sanitize_simulator_draft_slots(stored_payload)

            drafts.append(
                {
                    "id": row["id"],
                    "name": row["draft_name"],
                    "season": row["season"],
                    "mode": mode,
                    "slots": slots,
                    "concept_slots": concept_slots,
                    "created_at": row["created_at"],
                }
            )
        return jsonify(drafts)

    payload = request.get_json(silent=True) or {}
    draft_name = (payload.get("name") or "").strip()
    season = normalize_season_value(payload.get("season", ""))
    mode = (payload.get("mode") or "full").strip() or "full"
    slots = _sanitize_simulator_draft_slots(payload.get("slots"))
    concept_slots = _sanitize_one_sided_concept_slots(payload.get("concept_slots"))

    if not draft_name:
        return jsonify({"error": "Draft name is required."}), 400
    if len(draft_name) > 80:
        return jsonify({"error": "Draft name must be 80 characters or less."}), 400
    if mode == "concept_one_sided":
        if not any(concept_slots.values()):
            return jsonify({"error": "Add at least one concept slot before saving."}), 400
    else:
        mode = "full"
        if not any(slots.values()):
            return jsonify({"error": "Add at least one draft hero before saving."}), 400

    draft_payload = {
        "mode": mode,
        "slots": slots,
        "concept_slots": concept_slots,
    }

    cursor = db.execute(
        """
        INSERT INTO team_saved_drafts (team_id, draft_name, season, draft_slots_json)
        VALUES (?, ?, ?, ?)
        """,
        (team_id, draft_name, season, json.dumps(draft_payload)),
    )
    db.commit()

    return jsonify(
        {
            "id": cursor.lastrowid,
            "name": draft_name,
            "season": season,
            "mode": mode,
            "slots": slots,
            "concept_slots": concept_slots,
        }
    ), 201


@app.route("/api/saved-drafts/<int:draft_id>", methods=["DELETE"])
def api_delete_saved_draft(draft_id: int):
    db = get_db()
    row = db.execute(
        "SELECT id FROM team_saved_drafts WHERE id = ?",
        (draft_id,),
    ).fetchone()
    if row is None:
        abort(404)

    db.execute("DELETE FROM team_saved_drafts WHERE id = ?", (draft_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/scrims/new")
def new_scrim():
    return redirect(f"{url_for('scrims')}#create-scrim")


@app.route("/tournaments")
def tournaments():
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    season_options = get_scrim_season_options(TOURNAMENT_MATCHES)
    has_unseasoned_matches = any(not normalize_season_value(match.get("season", "")) for match in TOURNAMENT_MATCHES)
    default_season = get_current_season_from_recent_scrim(TOURNAMENT_MATCHES)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_matches,
        default_season=default_season,
    )
    selected_team_id = (request.args.get("team_id", "") or "").strip()

    filtered_matches = filter_scrims_by_season(TOURNAMENT_MATCHES, selected_season)
    if selected_team_id:
        filtered_matches = [match for match in filtered_matches if str(match.get("team_id") or "") == selected_team_id]

    return render_template(
        "tournaments.html",
        tournaments=filtered_matches,
        all_tournaments=list(reversed(TOURNAMENT_MATCHES)),
        teams=teams,
        today=date.today().isoformat(),
        season_options=season_options,
        selected_season=selected_season,
        selected_team_id=selected_team_id,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
        has_unseasoned_tournaments=has_unseasoned_matches,
        total_tournament_count=len(TOURNAMENT_MATCHES),
    )


@app.route("/tournaments/new")
def new_tournament():
    return redirect(f"{url_for('tournaments')}#create-tournament")


@app.route("/tournaments/create", methods=["POST"])
def create_tournament():
    global NEXT_TOURNAMENT_ID

    team_id = parse_team_id(request.form.get("team_id", ""))
    team_name = get_team_name_by_id(team_id)
    team_slot = normalize_match_team_slot(request.form.get("team_slot", "team1"))

    tournament_name = request.form.get("tournament_name", "").strip()
    if not tournament_name:
        flash("Please enter a tournament name.", "error")
        return redirect(f"{url_for('tournaments')}#create-tournament")

    match_date = request.form.get("scrim_date", "").strip()
    season = normalize_season_value(request.form.get("season", ""))
    notes = request.form.get("notes", "").strip()
    if not season:
        flash("Please set a season for this tournament.", "error")
        return redirect(f"{url_for('tournaments')}#create-tournament")

    tournament_match = {
        "id": NEXT_TOURNAMENT_ID,
        "tournament_name": tournament_name,
        "scrim_date": match_date,
        "season": season,
        "team_id": team_id,
        "team_name": team_name,
        "team_slot": team_slot,
        "tournament_teams": [],
        "team1_enemy_id": None,
        "team1_name": "",
        "team1_players": [],
        "team2_enemy_id": None,
        "team2_name": "",
        "team2_players": [],
        "notes": notes,
        "maps": [],
        "matches": [],
    }

    TOURNAMENT_MATCHES.append(tournament_match)
    NEXT_TOURNAMENT_ID += 1
    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_match["id"]))


@app.route("/scrims/create", methods=["POST"])
def create_scrim():
    global NEXT_SCRIM_ID

    team1_id = parse_team_id(request.form.get("team1_id", ""))
    team2_id = parse_team_id(request.form.get("team2_id", ""))
    team1_name = get_team_name_by_id(team1_id)
    team2_name = get_team_name_by_id(team2_id)
    if not team1_name or not team2_name:
        flash("Please select both teams for this scrim.", "error")
        return redirect(f"{url_for('scrims')}#create-scrim")
    if team1_id == team2_id:
        flash("Scrim teams must be different.", "error")
        return redirect(f"{url_for('scrims')}#create-scrim")

    scrim_date = request.form.get("scrim_date", "").strip()
    season = normalize_season_value(request.form.get("season", ""))
    notes = request.form.get("notes", "").strip()

    if not season:
        flash("Please set a season for this scrim.", "error")
        return redirect(f"{url_for('scrims')}#create-scrim")

    scrim = {
        "id": NEXT_SCRIM_ID,
        "opponent": team2_name,
        "enemy_team": team2_name,
        "enemy_team_id": team2_id,
        "scrim_date": scrim_date,
        "season": season,
        "team_id": team1_id,
        "team_name": team1_name,
        "team_slot": "team1",
        "team1_id": team1_id,
        "team1_name": team1_name,
        "team2_id": team2_id,
        "team2_name": team2_name,
        "notes": notes,
        "maps": [],
    }

    SCRIMS.append(scrim)
    NEXT_SCRIM_ID += 1
    save_app_state()

    return redirect(url_for("scrim_detail", scrim_id=scrim["id"]))


@app.route("/scrims/<int:scrim_id>")
def scrim_detail(scrim_id: int):
    scrim = get_scrim_or_404(scrim_id)
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    participant_one_label, participant_two_label = get_scrim_participant_labels(scrim)
    participant_one, participant_two = get_scrim_participants(scrim)

    participant_one_id = participant_one.get("id")
    participant_two_id = participant_two.get("id")
    participant_one_name = (participant_one_label or "").strip().lower()
    participant_two_name = (participant_two_label or "").strip().lower()

    def _resolve_participant_slot(map_entry: dict, participant_id: int | None, participant_name: str, fallback_slot: str) -> str:
        if participant_id is not None:
            if map_entry.get("team1_id") == participant_id:
                return "team1"
            if map_entry.get("team2_id") == participant_id:
                return "team2"

        team1_name = (map_entry.get("team1_name") or "").strip().lower()
        team2_name = (map_entry.get("team2_name") or "").strip().lower()
        if participant_name:
            if team1_name == participant_name:
                return "team1"
            if team2_name == participant_name:
                return "team2"

        return fallback_slot

    team1_score = 0
    team2_score = 0
    for map_entry in scrim.get("maps", []):
        left_raw, right_raw = split_score_pair(map_entry.get("score", ""))
        participant_one_slot = _resolve_participant_slot(map_entry, participant_one_id, participant_one_name, "team1")
        participant_two_slot = "team2" if participant_one_slot == "team1" else "team1"

        participant_one_outcome = get_map_outcome_for_slot(map_entry, participant_one_slot)
        if participant_one_outcome == "Win":
            team1_score += 1
            map_entry["participant_winner_label"] = participant_one_label
        elif participant_one_outcome == "Loss":
            team2_score += 1
            map_entry["participant_winner_label"] = participant_two_label
        else:
            map_entry["participant_winner_label"] = "Tie"

        if participant_one_slot == "team1":
            map_entry["participant_one_score"] = left_raw.strip()
            map_entry["participant_two_score"] = right_raw.strip()
        else:
            map_entry["participant_one_score"] = right_raw.strip()
            map_entry["participant_two_score"] = left_raw.strip()

        map_entry["participant_one_slot"] = participant_one_slot
        map_entry["participant_two_slot"] = participant_two_slot

    winner_label = "Tie"
    if team1_score > team2_score:
        winner_label = participant_one_label
    elif team2_score > team1_score:
        winner_label = participant_two_label

    return render_template(
        "scrim_detail.html",
        scrim=scrim,
        maps=MAPS,
        map_types=MAP_TYPES,
        map_images=MAP_IMAGES,
        map_submaps=MAP_SUBMAPS,
        sides=SIDES,
        results=RESULTS,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        teams=teams,
        team1_score=team1_score,
        team2_score=team2_score,
        winner_label=winner_label,
        match_label="Scrim",
        match_list_endpoint="scrims",
        match_detail_endpoint="scrim_detail",
        match_edit_endpoint="edit_scrim",
        match_delete_endpoint="delete_scrim",
        add_map_endpoint="add_map",
        map_detail_endpoint="map_detail",
        delete_map_endpoint="delete_map",
        participant_one_label=participant_one_label,
        participant_two_label=participant_two_label,
        participant_one_id=participant_one.get("id"),
        participant_two_id=participant_two.get("id"),
        split_score_pair=split_score_pair,
        opponent_field_label="Enemy Team",
        show_team_selector=True,
        attack_defense_maps=sorted(ATTACK_DEFENSE_MAPS),
    )


@app.route("/tournaments/<int:tournament_id>")
def tournament_detail(tournament_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    selected_perspective = normalize_match_team_slot(tournament_record.get("team_slot", "team1"))
    match_summaries = build_tournament_match_summaries(tournament_record)
    overview_analytics = build_tournament_overview_analytics(tournament_record)
    tournament_ban_analytics = build_scrim_analytics(build_tournament_match_scrims(tournament_record, selected_perspective))
    total_maps = sum(summary["maps"] for summary in match_summaries)
    completed_maps = sum(summary["completed_maps"] for summary in match_summaries)

    return render_template(
        "tournament_detail.html",
        tournament=tournament_record,
        teams=teams,
        match_summaries=match_summaries,
        overview_analytics=overview_analytics,
        tournament_ban_analytics=tournament_ban_analytics,
        selected_perspective=selected_perspective,
        total_maps=total_maps,
        completed_maps=completed_maps,
    )


@app.route("/tournaments/<int:tournament_id>/matches/add", methods=["POST"])
def add_tournament_match(tournament_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    team1_tournament_team_id = parse_team_id(request.form.get("team1_tournament_team_id", ""))
    team2_tournament_team_id = parse_team_id(request.form.get("team2_tournament_team_id", ""))
    team1 = get_tournament_team_by_id(tournament_record, team1_tournament_team_id)
    team2 = get_tournament_team_by_id(tournament_record, team2_tournament_team_id)

    if team1 is None or team2 is None:
        flash("Select two tournament teams for the match.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))
    if team1_tournament_team_id == team2_tournament_team_id:
        flash("Match teams must be different.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    tournament_match = {
        "id": next_tournament_match_id(tournament_record),
        "scrim_date": request.form.get("scrim_date", tournament_record.get("scrim_date", "")).strip(),
        "notes": request.form.get("notes", "").strip(),
        "team1_tournament_team_id": team1_tournament_team_id,
        "team2_tournament_team_id": team2_tournament_team_id,
        "team1_name": team1["name"],
        "team2_name": team2["name"],
        "maps": [],
    }
    tournament_record.setdefault("matches", []).append(tournament_match)
    save_app_state()
    return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=tournament_match["id"]))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>")
def tournament_match_detail(tournament_id: int, match_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    team1_map_wins = 0
    team2_map_wins = 0
    team1_tournament_team_id = tournament_match.get("team1_tournament_team_id")
    team2_tournament_team_id = tournament_match.get("team2_tournament_team_id")
    for map_entry in tournament_match.get("maps", []):
        winner_tournament_team_id = None
        left_raw, right_raw = split_score_pair(map_entry.get("score", ""))
        if left_raw and right_raw:
            try:
                left_score = float(left_raw)
                right_score = float(right_raw)
            except ValueError:
                left_score = right_score = 0.0
            if left_score > right_score:
                winner_tournament_team_id = map_entry.get("team1_tournament_team_id")
            elif right_score > left_score:
                winner_tournament_team_id = map_entry.get("team2_tournament_team_id")

        if winner_tournament_team_id is None:
            map_result = str(map_entry.get("result", "")).strip()
            if map_result == "Win":
                winner_tournament_team_id = map_entry.get("team1_tournament_team_id")
            elif map_result == "Loss":
                winner_tournament_team_id = map_entry.get("team2_tournament_team_id")

        if winner_tournament_team_id == team1_tournament_team_id:
            team1_map_wins += 1
        elif winner_tournament_team_id == team2_tournament_team_id:
            team2_map_wins += 1

    winner_label = "Tie"
    if team1_map_wins > team2_map_wins:
        winner_label = tournament_match.get("team1_name") or "Team 1"
    elif team2_map_wins > team1_map_wins:
        winner_label = tournament_match.get("team2_name") or "Team 2"

    return render_template(
        "tournament_match_detail.html",
        tournament=tournament_record,
        match=tournament_match,
        team1_map_wins=team1_map_wins,
        team2_map_wins=team2_map_wins,
        winner_label=winner_label,
        maps=MAPS,
        map_images=MAP_IMAGES,
        map_submaps=MAP_SUBMAPS,
        sides=SIDES,
        results=RESULTS,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        split_score_pair=split_score_pair,
    )


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/edit", methods=["POST"])
def edit_tournament_match(tournament_id: int, match_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    team1_tournament_team_id = parse_team_id(request.form.get("team1_tournament_team_id", ""))
    team2_tournament_team_id = parse_team_id(request.form.get("team2_tournament_team_id", ""))
    team1 = get_tournament_team_by_id(tournament_record, team1_tournament_team_id)
    team2 = get_tournament_team_by_id(tournament_record, team2_tournament_team_id)

    if team1 is None or team2 is None:
        flash("Select two tournament teams for the match.", "error")
        return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))
    if team1_tournament_team_id == team2_tournament_team_id:
        flash("Match teams must be different.", "error")
        return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))

    tournament_match["scrim_date"] = request.form.get("scrim_date", tournament_match.get("scrim_date", "")).strip()
    tournament_match["notes"] = request.form.get("notes", tournament_match.get("notes", "")).strip()
    tournament_match["team1_tournament_team_id"] = team1_tournament_team_id
    tournament_match["team2_tournament_team_id"] = team2_tournament_team_id
    tournament_match["team1_name"] = team1["name"]
    tournament_match["team2_name"] = team2["name"]

    for map_entry in tournament_match.get("maps", []):
        map_entry["team1_tournament_team_id"] = team1_tournament_team_id
        map_entry["team2_tournament_team_id"] = team2_tournament_team_id
        map_entry["team1_name"] = team1["name"]
        map_entry["team2_name"] = team2["name"]
        if map_entry.get("picked_by_tournament_team_id") not in {team1_tournament_team_id, team2_tournament_team_id}:
            map_entry["picked_by_tournament_team_id"] = None
            map_entry["picked_by_name"] = ""

    save_app_state()
    return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/delete", methods=["POST"])
def delete_tournament_match(tournament_id: int, match_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    tournament_record.setdefault("matches", []).remove(tournament_match)
    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/add-map", methods=["POST"])
def add_tournament_match_map(tournament_id: int, match_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)

    map_entry = build_match_map_entry_from_form()
    side1_tournament_team_id = parse_team_id(request.form.get("map_team1_tournament_team_id", ""))
    valid_team_ids = {
        tournament_match.get("team1_tournament_team_id"),
        tournament_match.get("team2_tournament_team_id"),
    }
    if side1_tournament_team_id not in valid_team_ids:
        flash("Choose which match team is on side 1 for this map.", "error")
        return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))

    side2_tournament_team_id = next(team_id for team_id in valid_team_ids if team_id != side1_tournament_team_id)
    team1 = get_tournament_team_by_id(tournament_record, side1_tournament_team_id)
    team2 = get_tournament_team_by_id(tournament_record, side2_tournament_team_id)
    map_entry["team1_tournament_team_id"] = side1_tournament_team_id
    map_entry["team2_tournament_team_id"] = side2_tournament_team_id
    map_entry["team1_name"] = team1.get("name", "") if team1 is not None else ""
    map_entry["team2_name"] = team2.get("name", "") if team2 is not None else ""

    picked_by_tournament_team_id = parse_team_id(request.form.get("picked_by_tournament_team_id", ""))
    if picked_by_tournament_team_id is not None:
        if picked_by_tournament_team_id not in {
            tournament_match.get("team1_tournament_team_id"),
            tournament_match.get("team2_tournament_team_id"),
        }:
            flash("Map picker must be one of the two match teams.", "error")
            return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))
        picker = get_tournament_team_by_id(tournament_record, picked_by_tournament_team_id)
        map_entry["picked_by_tournament_team_id"] = picked_by_tournament_team_id
        map_entry["picked_by_name"] = picker.get("name", "") if picker is not None else ""
    else:
        map_entry["picked_by_tournament_team_id"] = None
        map_entry["picked_by_name"] = ""

    tournament_match.setdefault("maps", []).append(map_entry)
    save_app_state()
    return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))


@app.route("/scrims/<int:scrim_id>/edit", methods=["POST"])
def edit_scrim(scrim_id: int):
    scrim = get_scrim_or_404(scrim_id)
    team1_id = parse_team_id(request.form.get("team1_id", ""))
    team2_id = parse_team_id(request.form.get("team2_id", ""))
    team1_name = get_team_name_by_id(team1_id)
    team2_name = get_team_name_by_id(team2_id)
    if not team1_name or not team2_name:
        flash("Please select both teams for this scrim.", "error")
        return redirect(url_for("scrim_detail", scrim_id=scrim_id))
    if team1_id == team2_id:
        flash("Scrim teams must be different.", "error")
        return redirect(url_for("scrim_detail", scrim_id=scrim_id))

    season = normalize_season_value(request.form.get("season", scrim.get("season", "")))
    if not season:
        flash("Please set a season for this scrim.", "error")
        return redirect(url_for("scrim_detail", scrim_id=scrim_id))

    scrim["opponent"] = team2_name
    scrim["enemy_team"] = team2_name
    scrim["enemy_team_id"] = team2_id
    scrim["scrim_date"] = request.form.get("scrim_date", scrim["scrim_date"]).strip()
    scrim["season"] = season
    scrim["team_id"] = team1_id
    scrim["team_name"] = team1_name
    scrim["team_slot"] = "team1"
    scrim["team1_id"] = team1_id
    scrim["team1_name"] = team1_name
    scrim["team2_id"] = team2_id
    scrim["team2_name"] = team2_name
    scrim["notes"] = request.form.get("notes", scrim["notes"]).strip()
    save_app_state()
    return redirect(url_for("scrim_detail", scrim_id=scrim_id))


@app.route("/tournaments/<int:tournament_id>/edit", methods=["POST"])
def edit_tournament(tournament_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    tournament_name = request.form.get("tournament_name", tournament_match.get("tournament_name", "")).strip()
    team_slot = normalize_match_team_slot(request.form.get("team_slot", tournament_match.get("team_slot", "team1")))
    if not tournament_name:
        flash("Please enter a tournament name.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    season = normalize_season_value(request.form.get("season", tournament_match.get("season", "")))
    if not season:
        flash("Please set a season for this tournament.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    tournament_match["tournament_name"] = tournament_name
    tournament_match["scrim_date"] = request.form.get("scrim_date", tournament_match.get("scrim_date", "")).strip()
    tournament_match["season"] = season
    tournament_match["team_slot"] = team_slot
    tournament_match["notes"] = request.form.get("notes", tournament_match.get("notes", "")).strip()
    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_id))


@app.route("/tournaments/<int:tournament_id>/teams", methods=["POST"])
def update_tournament_teams(tournament_id: int):
    tournament_match = get_tournament_or_404(tournament_id)

    team1_name = request.form.get("team1_name", tournament_match.get("team1_name", "")).strip()
    team2_name = request.form.get("team2_name", tournament_match.get("team2_name", "")).strip()
    if team1_name and team2_name and team1_name.lower() == team2_name.lower():
        flash("Tournament teams must be different.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    team1_players = parse_name_list(request.form.get("team1_players", ""))
    team2_players = parse_name_list(request.form.get("team2_players", ""))

    tournament_match["team1_name"] = team1_name
    tournament_match["team2_name"] = team2_name
    tournament_match["team1_enemy_id"] = None
    tournament_match["team2_enemy_id"] = None
    tournament_match["team1_players"] = team1_players
    tournament_match["team2_players"] = team2_players

    upsert_team_and_players(team1_name, team1_players)
    upsert_team_and_players(team2_name, team2_players)

    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_id))


@app.route("/tournaments/<int:tournament_id>/teams/add", methods=["POST"])
def add_tournament_team(tournament_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    source_team_id = parse_team_id(request.form.get("source_team_id", ""))

    if source_team_id is not None:
        source_team = get_db().execute(
            "SELECT id, name FROM teams WHERE id = ?",
            (source_team_id,),
        ).fetchone()
        if source_team is None:
            flash("Selected database team could not be found.", "error")
            return redirect(url_for("tournament_detail", tournament_id=tournament_id))

        team_name = (source_team["name"] or "").strip()
        players = [
            row["name"]
            for row in get_db().execute(
                "SELECT name FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
                (source_team_id,),
            ).fetchall()
        ]
    else:
        team_name = request.form.get("team_name", "").strip()
        if not team_name:
            flash("Please enter a tournament team name.", "error")
            return redirect(url_for("tournament_detail", tournament_id=tournament_id))
        players = parse_name_list(request.form.get("players", ""))

    existing_names = {str(team.get("name", "")).strip().lower() for team in tournament_match.get("tournament_teams", [])}
    if team_name.lower() in existing_names:
        flash("That tournament team already exists.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    new_tournament_team = {
        "id": next_tournament_team_id(tournament_match),
        "name": team_name,
        "players": players,
    }
    if source_team_id is not None:
        new_tournament_team["source_team_id"] = source_team_id

    tournament_match.setdefault("tournament_teams", []).append(new_tournament_team)
    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_id))


@app.route("/tournaments/<int:tournament_id>/teams/<int:tournament_team_id>/delete", methods=["POST"])
def delete_tournament_team(tournament_id: int, tournament_team_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    target_team = get_tournament_team_by_id(tournament_match, tournament_team_id)
    if target_team is None:
        abort(404)

    linked_match = next(
        (
            match for match in tournament_match.get("matches", [])
            if match.get("team1_tournament_team_id") == tournament_team_id
            or match.get("team2_tournament_team_id") == tournament_team_id
        ),
        None,
    )
    if linked_match is not None:
        flash("Remove this team from its tournament matches before deleting it.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    tournament_match["tournament_teams"] = [
        team for team in tournament_match.get("tournament_teams", []) if team.get("id") != tournament_team_id
    ]
    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_id))


@app.route("/scrims/<int:scrim_id>/delete", methods=["POST"])
def delete_scrim(scrim_id: int):
    scrim = get_scrim_or_404(scrim_id)
    SCRIMS.remove(scrim)
    save_app_state(allow_scrim_removal=True)
    return redirect(url_for("scrims"))


@app.route("/tournaments/<int:tournament_id>/delete", methods=["POST"])
def delete_tournament(tournament_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    TOURNAMENT_MATCHES.remove(tournament_match)
    save_app_state()
    return redirect(url_for("tournaments"))


@app.route("/scrims/<int:scrim_id>/add-map", methods=["POST"])
def add_map(scrim_id: int):
    scrim = get_scrim_or_404(scrim_id)

    map_entry = build_match_map_entry_from_form()
    participant_one, participant_two = get_scrim_participants(scrim)
    valid_team_ids = {
        participant_one.get("id"),
        participant_two.get("id"),
    }
    side1_team_id = parse_team_id(request.form.get("map_team1_team_id", ""))

    if side1_team_id in valid_team_ids and participant_one.get("id") and participant_two.get("id"):
        if side1_team_id == participant_one.get("id"):
            side1_team = participant_one
            side2_team = participant_two
        else:
            side1_team = participant_two
            side2_team = participant_one
    else:
        side1_team = participant_one
        side2_team = participant_two

    map_entry["team1_id"] = side1_team.get("id")
    map_entry["team2_id"] = side2_team.get("id")
    map_entry["team1_name"] = side1_team.get("name", "")
    map_entry["team2_name"] = side2_team.get("name", "")
    map_entry["our_team_slot"] = "team1" if side1_team.get("id") == participant_one.get("id") else "team2"
    inferred_result = infer_result_from_score_text(map_entry.get("score", ""), slot=map_entry["our_team_slot"])
    if inferred_result:
        map_entry["result"] = inferred_result

    scrim["maps"].append(map_entry)
    save_app_state()

    return redirect(url_for("scrim_detail", scrim_id=scrim_id))


@app.route("/tournaments/<int:tournament_id>/add-map", methods=["POST"])
def add_tournament_map(tournament_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    team1_tournament_team_id = parse_team_id(request.form.get("team1_tournament_team_id", ""))
    team2_tournament_team_id = parse_team_id(request.form.get("team2_tournament_team_id", ""))
    team1 = get_tournament_team_by_id(tournament_match, team1_tournament_team_id)
    team2 = get_tournament_team_by_id(tournament_match, team2_tournament_team_id)
    if team1 is None or team2 is None:
        flash("Select two tournament teams before adding a map.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))
    if team1_tournament_team_id == team2_tournament_team_id:
        flash("Map teams must be different.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    map_entry = build_match_map_entry_from_form()
    map_entry["team1_tournament_team_id"] = team1_tournament_team_id
    map_entry["team2_tournament_team_id"] = team2_tournament_team_id
    map_entry["team1_name"] = team1["name"]
    map_entry["team2_name"] = team2["name"]
    map_entry["our_team_slot"] = "team1"
    inferred_result = infer_result_from_score_text(map_entry.get("score", ""), slot=map_entry["our_team_slot"])
    if inferred_result:
        map_entry["result"] = inferred_result
    tournament_match["maps"].append(map_entry)
    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>")
def map_detail(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)

    context = build_match_map_detail_context(scrim, map_entry, is_tournament=False)

    return render_template(
        "map_detail.html",
        scrim=scrim,
        is_tournament=False,
        back_to_detail_endpoint="scrim_detail",
        match_detail_endpoint="map_detail",
        delete_map_endpoint="delete_map",
        update_draft_endpoint="update_draft",
        update_notes_endpoint="update_notes",
        update_vod_endpoint="update_vod",
        update_map_info_endpoint="update_map_info",
        update_comp_endpoint="update_comp",
        update_comp_section_endpoint="update_comp_section",
        add_comp_section_endpoint="add_comp_section",
        delete_event_endpoint="delete_event",
        add_event_endpoint="add_event_to_map",
        detail_parent_id=scrim_id,
        detail_match_id=scrim_id,
        **context,
    )


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>")
def tournament_match_map_detail(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)

    context = build_match_map_detail_context(
        tournament_match,
        map_entry,
        is_tournament=True,
        tournament_record=tournament_record,
    )

    return render_template(
        "map_detail.html",
        scrim=tournament_match,
        tournament=tournament_record,
        is_tournament=True,
        back_to_detail_endpoint="tournament_match_detail",
        match_detail_endpoint="tournament_match_map_detail",
        delete_map_endpoint="delete_tournament_match_map",
        update_draft_endpoint="update_tournament_match_draft",
        update_notes_endpoint="update_tournament_match_notes",
        update_vod_endpoint="update_tournament_match_vod",
        update_map_info_endpoint="update_tournament_match_map_info",
        update_comp_endpoint="update_tournament_match_comp",
        update_comp_section_endpoint="update_tournament_match_comp_section",
        delete_event_endpoint="delete_tournament_match_event",
        add_event_endpoint="add_tournament_match_event_to_map",
        detail_parent_id=tournament_id,
        detail_match_id=match_id,
        **context,
    )


@app.route("/scrims/<int:scrim_id>/timelines")
def scrim_timelines(scrim_id: int):
    return redirect(url_for("scrim_detail", scrim_id=scrim_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/timelines")
def tournament_match_timelines(tournament_id: int, match_id: int):
    return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))


@app.route("/scrims/<int:scrim_id>/timelines/<path:map_name>")
def scrim_map_timeline(scrim_id: int, map_name: str):
    scrim = get_scrim_or_404(scrim_id)
    participant_one_label, participant_two_label = get_scrim_participant_labels(scrim)

    team_id = scrim.get("team_id")
    team_name = (scrim.get("team_name") or scrim.get("team1_name") or "").strip()
    map_timeline_row = None
    map_overview = {
        "maps": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0,
    }
    top_hero_rows: list[dict] = []
    enemy_top_hero_rows: list[dict] = []
    if team_id and team_name:
        source_scrims = get_scrims_for_team(team_id, team_name)
        draft_timeline = build_draft_phase_timeline(source_scrims)
        map_timeline_row = next(
            (row for row in draft_timeline.get("maps", []) if row.get("map_name") == map_name),
            None,
        )

        our_hero_counts = defaultdict(int)
        our_hero_win_counts = defaultdict(int)
        enemy_hero_counts = defaultdict(int)
        total_our_instances = 0
        total_enemy_instances = 0
        map_count = 0
        win_count = 0
        loss_count = 0
        for source_scrim in source_scrims:
            for map_entry in source_scrim.get("maps", []):
                if (map_entry.get("map_name") or "").strip() != map_name:
                    continue
                map_count += 1
                our_team_slot = map_entry.get("our_team_slot", "team1")
                if our_team_slot not in TEAM_SLOTS:
                    our_team_slot = "team1"
                enemy_team_slot = opposite_team_slot(our_team_slot)

                result = get_map_outcome_for_slot(map_entry, our_team_slot)
                is_win = result == "Win"
                if is_win:
                    win_count += 1
                elif result == "Loss":
                    loss_count += 1

                heroes_in_map = _canonical_map_hero_instances(map_entry, our_team_slot)
                enemy_heroes_in_map = _canonical_map_hero_instances(map_entry, enemy_team_slot)
                total_our_instances += len(heroes_in_map)
                total_enemy_instances += len(enemy_heroes_in_map)

                for hero_name in heroes_in_map:
                    our_hero_counts[hero_name] += 1
                    if is_win:
                        our_hero_win_counts[hero_name] += 1
                for hero_name in enemy_heroes_in_map:
                    enemy_hero_counts[hero_name] += 1

        map_overview = {
            "maps": map_count,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": round((win_count / map_count) * 100, 1) if map_count else 0,
        }
        top_hero_rows = [
            {
                "hero": hero_name,
                "appearances": hero_maps,
                "play_rate": round((hero_maps / total_our_instances) * 100, 1) if total_our_instances else 0,
                "win_rate": round((our_hero_win_counts.get(hero_name, 0) / hero_maps) * 100, 1) if hero_maps else 0,
            }
            for hero_name, hero_maps in sorted(our_hero_counts.items(), key=lambda item: (-item[1], item[0].lower()))[:12]
        ]
        enemy_top_hero_rows = [
            {
                "hero": hero_name,
                "appearances": hero_maps,
                "play_rate": round((hero_maps / total_enemy_instances) * 100, 1) if total_enemy_instances else 0,
            }
            for hero_name, hero_maps in sorted(enemy_hero_counts.items(), key=lambda item: (-item[1], item[0].lower()))[:12]
        ]

    return render_template(
        "map_timeline_detail.html",
        map_name=map_name,
        map_timeline_row=map_timeline_row,
        map_overview=map_overview,
        top_hero_rows=top_hero_rows,
        enemy_top_hero_rows=enemy_top_hero_rows,
        participant_one_label=participant_one_label,
        participant_two_label=participant_two_label,
        is_tournament=False,
        back_to_maps_url=(url_for("team_detail", team_id=scrim.get("team_id")) + "#maps") if scrim.get("team_id") else url_for("teams"),
    )


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/timelines/<path:map_name>")
def tournament_match_map_timeline(tournament_id: int, match_id: int, map_name: str):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)

    perspective = tournament_match.get("our_team_slot", "team1") if tournament_match.get("our_team_slot", "team1") in TEAM_SLOTS else "team1"
    source_scrims = build_tournament_match_scrims(tournament_record, perspective=perspective)
    draft_timeline = build_draft_phase_timeline(source_scrims)
    map_timeline_row = next(
        (row for row in draft_timeline.get("maps", []) if row.get("map_name") == map_name),
        None,
    )

    our_hero_counts = defaultdict(int)
    our_hero_win_counts = defaultdict(int)
    enemy_hero_counts = defaultdict(int)
    map_count = 0
    win_count = 0
    loss_count = 0
    for source_scrim in source_scrims:
        for map_entry in source_scrim.get("maps", []):
            if (map_entry.get("map_name") or "").strip() != map_name:
                continue
            map_count += 1
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            enemy_team_slot = opposite_team_slot(our_team_slot)

            result = get_map_outcome_for_slot(map_entry, our_team_slot)
            is_win = result == "Win"
            if is_win:
                win_count += 1
            elif result == "Loss":
                loss_count += 1

            heroes_in_map = set()
            enemy_heroes_in_map = set()
            for section in map_entry.get("comp", []):
                for slot in section.get(our_team_slot, []):
                    hero_name = _resolve_hero_transform_key((slot.get("hero") or "").strip()) or (slot.get("hero") or "").strip()
                    if hero_name:
                        heroes_in_map.add(hero_name)
                for slot in section.get(enemy_team_slot, []):
                    hero_name = _resolve_hero_transform_key((slot.get("hero") or "").strip()) or (slot.get("hero") or "").strip()
                    if hero_name:
                        enemy_heroes_in_map.add(hero_name)

            for hero_name in heroes_in_map:
                our_hero_counts[hero_name] += 1
                if is_win:
                    our_hero_win_counts[hero_name] += 1
            for hero_name in enemy_heroes_in_map:
                enemy_hero_counts[hero_name] += 1

    map_overview = {
        "maps": map_count,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": round((win_count / map_count) * 100, 1) if map_count else 0,
    }
    total_our_instances = sum(our_hero_counts.values())
    total_enemy_instances = sum(enemy_hero_counts.values())
    top_hero_rows = [
        {
            "hero": hero_name,
            "appearances": hero_maps,
            "play_rate": round((hero_maps / total_our_instances) * 100, 1) if total_our_instances else 0,
            "win_rate": round((our_hero_win_counts.get(hero_name, 0) / hero_maps) * 100, 1) if hero_maps else 0,
        }
        for hero_name, hero_maps in sorted(our_hero_counts.items(), key=lambda item: (-item[1], item[0].lower()))[:12]
    ]
    enemy_top_hero_rows = [
        {
            "hero": hero_name,
            "appearances": hero_maps,
            "play_rate": round((hero_maps / total_enemy_instances) * 100, 1) if total_enemy_instances else 0,
        }
        for hero_name, hero_maps in sorted(enemy_hero_counts.items(), key=lambda item: (-item[1], item[0].lower()))[:12]
    ]

    team1_label = (get_tournament_team_by_id(tournament_record, tournament_match.get("team1_tournament_team_id")) or {}).get("name") or tournament_match.get("team1_name") or "Team 1"
    team2_label = (get_tournament_team_by_id(tournament_record, tournament_match.get("team2_tournament_team_id")) or {}).get("name") or tournament_match.get("team2_name") or "Team 2"

    return render_template(
        "map_timeline_detail.html",
        map_name=map_name,
        map_timeline_row=map_timeline_row,
        map_overview=map_overview,
        top_hero_rows=top_hero_rows,
        enemy_top_hero_rows=enemy_top_hero_rows,
        participant_one_label=team1_label,
        participant_two_label=team2_label,
        is_tournament=True,
        back_to_maps_url=(
            url_for("tournament_team_detail", tournament_id=tournament_id, tournament_team_id=parse_team_id(request.args.get("tournament_team_id", ""))) + "#maps"
            if parse_team_id(request.args.get("tournament_team_id", "")) is not None
            else url_for("tournament_detail", tournament_id=tournament_id)
        ),
    )


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/delete", methods=["POST"])
def delete_tournament_match_map(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    tournament_match["maps"].remove(map_entry)
    save_app_state()
    return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/update-draft", methods=["POST"])
def update_tournament_match_draft(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)

    map_entry["draft"] = {
        "team1": {
            "ban1": request.form.get("team1_ban1", "").strip(),
            "protect1": request.form.get("team1_protect1", "").strip(),
            "ban2": request.form.get("team1_ban2", "").strip(),
            "ban3": request.form.get("team1_ban3", "").strip(),
            "ban4": request.form.get("team1_ban4", "").strip(),
            "protect2": request.form.get("team1_protect2", "").strip(),
        },
        "team2": {
            "ban1": request.form.get("team2_ban1", "").strip(),
            "ban2": request.form.get("team2_ban2", "").strip(),
            "protect1": request.form.get("team2_protect1", "").strip(),
            "ban3": request.form.get("team2_ban3", "").strip(),
            "protect2": request.form.get("team2_protect2", "").strip(),
            "ban4": request.form.get("team2_ban4", "").strip(),
        },
    }
    save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/update-notes", methods=["POST"])
def update_tournament_match_notes(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    map_entry["notes"] = request.form.get("notes", "").strip()
    save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/update-vod", methods=["POST"])
def update_tournament_match_vod(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    map_entry["vod_url"] = request.form.get("vod_url", "").strip()
    save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/update-info", methods=["POST"])
def update_tournament_match_map_info(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    
    # Get score and auto-calculate result if not provided
    score_team1 = request.form.get("score_team1", "").strip()
    score_team2 = request.form.get("score_team2", "").strip()
    score = request.form.get("score", "").strip()
    
    # Prefer team-specific scores if provided
    if score_team1 or score_team2:
        map_entry["score"] = f"{score_team1}-{score_team2}".strip("-")
    else:
        map_entry["score"] = score
    
    # Manual result selection (no auto-calculation from score)
    result = request.form.get("result", "").strip()
    map_entry["result"] = result if result in RESULTS else ""
    if not map_entry["result"] and map_entry.get("score"):
        inferred = infer_result_from_score_text(
            map_entry.get("score", ""),
            slot=map_entry.get("our_team_slot", "team1"),
        )
        if inferred in RESULTS:
            map_entry["result"] = inferred

    raw_map_team1_id = request.form.get("map_team1_tournament_team_id", "")
    if raw_map_team1_id:
        side1_tournament_team_id = parse_team_id(raw_map_team1_id)
        valid_team_ids = {
            tournament_match.get("team1_tournament_team_id"),
            tournament_match.get("team2_tournament_team_id"),
        }
        if side1_tournament_team_id not in valid_team_ids:
            flash("Choose a valid match team for side 1.", "error")
            return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))
        remaining_team_ids = [team_id for team_id in valid_team_ids if team_id is not None and team_id != side1_tournament_team_id]
        if not remaining_team_ids:
            flash("Match teams are not configured correctly. Please set two different teams on the match.", "error")
            return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))
        side2_tournament_team_id = remaining_team_ids[0]
        team1 = get_tournament_team_by_id(tournament_record, side1_tournament_team_id)
        team2 = get_tournament_team_by_id(tournament_record, side2_tournament_team_id)
        map_entry["team1_tournament_team_id"] = side1_tournament_team_id
        map_entry["team2_tournament_team_id"] = side2_tournament_team_id
        map_entry["team1_name"] = team1.get("name", "") if team1 is not None else ""
        map_entry["team2_name"] = team2.get("name", "") if team2 is not None else ""
        map_entry["our_team_slot"] = "team1" if side1_tournament_team_id == tournament_match.get("team1_tournament_team_id") else "team2"
        inferred_result = infer_result_from_score_text(map_entry.get("score", ""), slot=map_entry["our_team_slot"])
        if inferred_result:
            map_entry["result"] = inferred_result

    picked_by_tournament_team_id = parse_team_id(request.form.get("picked_by_tournament_team_id", ""))
    if picked_by_tournament_team_id is not None:
        if picked_by_tournament_team_id not in {
            tournament_match.get("team1_tournament_team_id"),
            tournament_match.get("team2_tournament_team_id"),
        }:
            flash("Map picker must be one of the two match teams.", "error")
            return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))
        picker = get_tournament_team_by_id(tournament_record, picked_by_tournament_team_id)
        map_entry["picked_by_tournament_team_id"] = picked_by_tournament_team_id
        map_entry["picked_by_name"] = picker.get("name", "") if picker is not None else ""
    else:
        map_entry["picked_by_tournament_team_id"] = None
        map_entry["picked_by_name"] = ""

    has_submaps = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), []))
    if has_submaps:
        map_entry["side"] = request.form.get("side", map_entry.get("side", "")).strip()
    else:
        map_entry["side"] = ""
    save_app_state()
    if request.form.get("next") == "match":
        return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/update-comp", methods=["POST"])
def update_tournament_match_comp(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    use_section_sides = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), [])) or map_entry.get("map_name") in ATTACK_DEFENSE_MAPS
    section_count = int(request.form.get("section_count", "1"))
    sections = []
    for s in range(section_count):
        side_value = request.form.get(f"sec_{s}_side", "").strip() if use_section_sides else ""
        if side_value not in SIDES:
            side_value = ""
        sec = {
            "submap": request.form.get(f"sec_{s}_submap", "").strip(),
            "side": side_value,
            "score": request.form.get(f"sec_{s}_score", "").strip(),
            "team1": [],
            "team2": [],
        }
        for i in range(6):
            sec["team1"].append({
                "hero": request.form.get(f"sec_{s}_team1_hero_{i}", "").strip(),
                "player": request.form.get(f"sec_{s}_team1_player_{i}", "").strip(),
            })
            sec["team2"].append({
                "hero": request.form.get(f"sec_{s}_team2_hero_{i}", "").strip(),
                "player": request.form.get(f"sec_{s}_team2_player_{i}", "").strip(),
            })
        auto_assign_section_players_from_heroes(
            tournament_match,
            map_entry,
            sec,
            is_tournament=True,
            tournament_record=tournament_record,
        )
        sections.append(sec)
    map_entry["comp"] = sections
    save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/update-comp-section/<int:section_index>", methods=["POST"])
def update_tournament_match_comp_section(tournament_id: int, match_id: int, map_id: int, section_index: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    while len(map_entry.get("comp", [])) <= section_index:
        map_entry.setdefault("comp", []).append(build_default_comp_sections(map_entry.get("map_name", ""))[0])

    section = map_entry["comp"][section_index]
    score_team1 = request.form.get("score_team1", "").strip()
    score_team2 = request.form.get("score_team2", "").strip()
    if score_team1 or score_team2:
        section["score"] = f"{score_team1}-{score_team2}".strip("-")
    else:
        section["score"] = ""
    side_value = request.form.get("side", section.get("side", "")).strip()
    if side_value not in SIDES:
        side_value = ""
    section["side"] = side_value
    
    # Store submap result if this section has a submap
    section_result = request.form.get("section_result", "").strip()
    if section_result in RESULTS:
        section["result"] = section_result
    elif section.get("submap"):
        inferred_submap_result = infer_result_from_score_text(section.get("score", ""), slot="team1")
        if inferred_submap_result in RESULTS:
            section["result"] = inferred_submap_result
        else:
            # Clear result if no valid result provided and score is not decisive.
            section.pop("result", None)

    for i in range(6):
        section["team1"][i]["hero"] = request.form.get(f"team1_hero_{i}", "").strip()
        section["team1"][i]["player"] = request.form.get(f"team1_player_{i}", "").strip()
        section["team2"][i]["hero"] = request.form.get(f"team2_hero_{i}", "").strip()
        section["team2"][i]["player"] = request.form.get(f"team2_player_{i}", "").strip()

    auto_assign_section_players_from_heroes(
        tournament_match,
        map_entry,
        section,
        is_tournament=True,
        tournament_record=tournament_record,
    )
    save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/delete-event/<int:event_id>", methods=["POST"])
def delete_tournament_match_event(tournament_id: int, match_id: int, map_id: int, event_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    event_to_delete = next((event for event in map_entry.get("events", []) if event.get("id") == event_id), None)
    if event_to_delete is None:
        abort(404)
    map_entry["events"].remove(event_to_delete)
    save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/add-event", methods=["POST"])
def add_tournament_match_event_to_map(tournament_id: int, match_id: int, map_id: int):
    global NEXT_EVENT_ID

    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    event_type = request.form.get("event_type", "").strip()
    if event_type not in EVENT_TYPES:
        flash("Please select a valid event type.", "error")
        return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))

    map_entry.setdefault("events", []).append(
        {
            "id": NEXT_EVENT_ID,
            "timestamp": request.form.get("timestamp", "").strip(),
            "event_type": event_type,
            "description": request.form.get("description", "").strip(),
        }
    )
    NEXT_EVENT_ID += 1
    save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>")
def tournament_map_detail(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)

    context = build_match_map_detail_context(tournament_match, map_entry, is_tournament=True)

    return render_template(
        "map_detail.html",
        scrim=tournament_match,
        is_tournament=True,
        back_to_detail_endpoint="tournament_detail",
        match_detail_endpoint="tournament_map_detail",
        delete_map_endpoint="delete_tournament_map",
        update_draft_endpoint="update_tournament_draft",
        update_notes_endpoint="update_tournament_notes",
        update_vod_endpoint="update_tournament_vod",
        update_map_info_endpoint="update_tournament_map_info",
        update_comp_endpoint="update_tournament_comp",
        update_comp_section_endpoint="update_tournament_comp_section",
        delete_event_endpoint="delete_tournament_event",
        add_event_endpoint="add_tournament_event_to_map",
        detail_parent_id=tournament_id,
        detail_match_id=tournament_id,
        **context,
    )

    if map_entry.get("our_team_slot") not in TEAM_SLOTS:
        map_entry["our_team_slot"] = "team1"

    # Migrate old dict-style comp to section list
    if "comp" not in map_entry or isinstance(map_entry["comp"], dict):
        old = map_entry.get("comp", {})
        if isinstance(old, dict) and "team1" in old:
            map_entry["comp"] = [{
                "submap": "",
                "side": map_entry.get("side", ""),
                "team1": old["team1"],
                "team2": old["team2"],
            }]
        else:
            map_entry["comp"] = build_default_comp_sections(map_entry["map_name"])

    use_section_sides = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), [])) or map_entry.get("map_name") in ATTACK_DEFENSE_MAPS
    for sec in map_entry.get("comp", []):
        sec.setdefault("submap", "")
        sec.setdefault("score", "")
        side_value = (sec.get("side", "") or "").strip()
        if not use_section_sides:
            side_value = ""
        elif side_value not in SIDES:
            side_value = ""
        sec["side"] = side_value

    # Get team roster for player selection
    db = get_db()
    team_id = scrim.get("team_id")
    team_players = []
    if team_id:
        player_rows = db.execute(
            "SELECT name FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
            (team_id,),
        ).fetchall()
        team_players = [row["name"] for row in player_rows]

    # Get enemy team info and players if available
    enemy_team_data = None
    enemy_players = []
    enemy_team_id = scrim.get("enemy_team_id")
    if enemy_team_id:
        enemy_team_rows = db.execute(
            "SELECT id, name, notes FROM enemy_teams WHERE id = ?",
            (enemy_team_id,),
        ).fetchone()
        if enemy_team_rows:
            enemy_team_data = dict(enemy_team_rows)
            enemy_player_rows = db.execute(
                "SELECT name, role, main_hero FROM enemy_players WHERE enemy_team_id = ? ORDER BY name COLLATE NOCASE",
                (enemy_team_id,),
            ).fetchall()
            enemy_players = [dict(row) for row in enemy_player_rows]

    return render_template(
        "map_detail.html",
        scrim=scrim,
        map_entry=map_entry,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        map_images=MAP_IMAGES,
        map_submaps=MAP_SUBMAPS,
        maps=MAPS,
        sides=SIDES,
        results=RESULTS,
        event_types=EVENT_TYPES,
        team_players=team_players,
        enemy_team=enemy_team_data,
        enemy_players=enemy_players,
    )


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/delete", methods=["POST"])
def delete_map(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    scrim["maps"].remove(map_entry)
    save_app_state()
    return redirect(url_for("scrim_detail", scrim_id=scrim_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/delete", methods=["POST"])
def delete_tournament_map(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    tournament_match["maps"].remove(map_entry)
    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-draft", methods=["POST"])
def update_draft(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)

    map_entry["draft"] = {
        "team1": {
            "ban1": request.form.get("team1_ban1", "").strip(),
            "protect1": request.form.get("team1_protect1", "").strip(),
            "ban2": request.form.get("team1_ban2", "").strip(),
            "ban3": request.form.get("team1_ban3", "").strip(),
            "ban4": request.form.get("team1_ban4", "").strip(),
            "protect2": request.form.get("team1_protect2", "").strip(),
        },
        "team2": {
            "ban1": request.form.get("team2_ban1", "").strip(),
            "ban2": request.form.get("team2_ban2", "").strip(),
            "protect1": request.form.get("team2_protect1", "").strip(),
            "ban3": request.form.get("team2_ban3", "").strip(),
            "protect2": request.form.get("team2_protect2", "").strip(),
            "ban4": request.form.get("team2_ban4", "").strip(),
        },
    }
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/update-draft", methods=["POST"])
def update_tournament_draft(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)

    map_entry["draft"] = {
        "team1": {
            "ban1": request.form.get("team1_ban1", "").strip(),
            "protect1": request.form.get("team1_protect1", "").strip(),
            "ban2": request.form.get("team1_ban2", "").strip(),
            "ban3": request.form.get("team1_ban3", "").strip(),
            "ban4": request.form.get("team1_ban4", "").strip(),
            "protect2": request.form.get("team1_protect2", "").strip(),
        },
        "team2": {
            "ban1": request.form.get("team2_ban1", "").strip(),
            "ban2": request.form.get("team2_ban2", "").strip(),
            "protect1": request.form.get("team2_protect1", "").strip(),
            "ban3": request.form.get("team2_ban3", "").strip(),
            "protect2": request.form.get("team2_protect2", "").strip(),
            "ban4": request.form.get("team2_ban4", "").strip(),
        },
    }
    save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-notes", methods=["POST"])
def update_notes(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    map_entry["notes"] = request.form.get("notes", "").strip()
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/update-notes", methods=["POST"])
def update_tournament_notes(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    map_entry["notes"] = request.form.get("notes", "").strip()
    save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-vod", methods=["POST"])
def update_vod(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    map_entry["vod_url"] = request.form.get("vod_url", "").strip()
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/update-vod", methods=["POST"])
def update_tournament_vod(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    map_entry["vod_url"] = request.form.get("vod_url", "").strip()
    save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-info", methods=["POST"])
def update_map_info(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    
    # Get team-specific scores and build combined score
    score_team1 = request.form.get("score_team1", "").strip()
    score_team2 = request.form.get("score_team2", "").strip()
    score = request.form.get("score", "").strip()
    our_atk = request.form.get("our_attack_score", "").strip()
    enemy_atk = request.form.get("enemy_attack_score", "").strip()

    # Attack/defense score input takes priority for non-control maps
    if our_atk or enemy_atk:
        map_entry["our_attack_score"] = our_atk
        map_entry["enemy_attack_score"] = enemy_atk
        our_team_slot = map_entry.get("our_team_slot", "team1")
        if our_team_slot == "team1":
            map_entry["score"] = f"{our_atk}-{enemy_atk}"
        else:
            map_entry["score"] = f"{enemy_atk}-{our_atk}"
    # Prefer team-specific scores if provided
    elif score_team1 or score_team2:
        map_entry["score"] = f"{score_team1}-{score_team2}".strip("-")
    else:
        map_entry["score"] = score
    
    # Manual result selection (no auto-calculation from score)
    result = request.form.get("result", "").strip()
    map_entry["result"] = result if result in RESULTS else ""

    # If result is left blank, infer from attack-defense scores directly (most reliable).
    if not map_entry["result"] and (our_atk or enemy_atk):
        try:
            o = int(our_atk) if our_atk else 0
            e = int(enemy_atk) if enemy_atk else 0
            if o > e:
                map_entry["result"] = "Win"
            elif e > o:
                map_entry["result"] = "Loss"
            else:
                map_entry["result"] = "Draw"
        except ValueError:
            pass

    # If result still blank, infer from score text string.
    if not map_entry["result"] and map_entry.get("score"):
        inferred = infer_result_from_score_text(
            map_entry.get("score", ""),
            slot=map_entry.get("our_team_slot", "team1"),
        )
        if inferred in RESULTS:
            map_entry["result"] = inferred
    
    participant_one, participant_two = get_scrim_participants(scrim)
    valid_team_ids = {
        participant_one.get("id"),
        participant_two.get("id"),
    }
    side1_team_id = parse_team_id(request.form.get("map_team1_team_id", ""))
    if side1_team_id in valid_team_ids and participant_one.get("id") and participant_two.get("id"):
        if side1_team_id == participant_one.get("id"):
            side1_team = participant_one
            side2_team = participant_two
        else:
            side1_team = participant_two
            side2_team = participant_one
        map_entry["team1_id"] = side1_team.get("id")
        map_entry["team2_id"] = side2_team.get("id")
        map_entry["team1_name"] = side1_team.get("name", "")
        map_entry["team2_name"] = side2_team.get("name", "")

    # Final side-name sync from ids so "Team on Side 1" stays authoritative.
    if map_entry.get("team1_id"):
        row = get_db().execute("SELECT name FROM teams WHERE id = ?", (map_entry["team1_id"],)).fetchone()
        if row is not None:
            map_entry["team1_name"] = row["name"]
    if map_entry.get("team2_id"):
        row = get_db().execute("SELECT name FROM teams WHERE id = ?", (map_entry["team2_id"],)).fetchone()
        if row is not None:
            map_entry["team2_name"] = row["name"]

    if (
        (map_entry.get("team1_name", "") or "").strip().lower() != (map_entry.get("team2_name", "") or "").strip().lower()
        and map_entry.get("team1_id")
        and map_entry.get("team1_id") == map_entry.get("team2_id")
    ):
        map_entry["team2_id"] = participant_two.get("id") if side1_team_id == participant_one.get("id") else participant_one.get("id")
        if map_entry.get("team2_id"):
            row = get_db().execute("SELECT name FROM teams WHERE id = ?", (map_entry["team2_id"],)).fetchone()
            if row is not None:
                map_entry["team2_name"] = row["name"]
    has_submaps = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), []))
    if has_submaps:
        map_entry["side"] = request.form.get("side", map_entry.get("side", "")).strip()
    else:
        map_entry["side"] = ""
    save_app_state()
    if request.form.get("next") == "scrim":
        return redirect(url_for("scrim_detail", scrim_id=scrim_id))
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/update-info", methods=["POST"])
def update_tournament_map_info(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    map_entry["result"] = request.form.get("result", map_entry["result"]).strip()
    if map_entry["result"] not in RESULTS:
        map_entry["result"] = ""
    map_entry["score"] = request.form.get("score", map_entry.get("score", "")).strip()
    if not map_entry["result"] and map_entry.get("score"):
        inferred = infer_result_from_score_text(
            map_entry.get("score", ""),
            slot=map_entry.get("our_team_slot", "team1"),
        )
        if inferred in RESULTS:
            map_entry["result"] = inferred
    raw_team1_id = request.form.get("team1_tournament_team_id", "")
    raw_team2_id = request.form.get("team2_tournament_team_id", "")
    if raw_team1_id or raw_team2_id:
        team1_tournament_team_id = parse_team_id(raw_team1_id)
        team2_tournament_team_id = parse_team_id(raw_team2_id)
        team1 = get_tournament_team_by_id(tournament_match, team1_tournament_team_id)
        team2 = get_tournament_team_by_id(tournament_match, team2_tournament_team_id)
        if team1 is None or team2 is None:
            flash("Select two tournament teams for this map.", "error")
            return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))
        if team1_tournament_team_id == team2_tournament_team_id:
            flash("Map teams must be different.", "error")
            return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))
        map_entry["team1_tournament_team_id"] = team1_tournament_team_id
        map_entry["team2_tournament_team_id"] = team2_tournament_team_id
        map_entry["team1_name"] = team1["name"]
        map_entry["team2_name"] = team2["name"]
    our_team_slot = request.form.get("our_team_slot", map_entry.get("our_team_slot", "team1")).strip()
    map_entry["our_team_slot"] = our_team_slot if our_team_slot in TEAM_SLOTS else "team1"
    has_submaps = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), []))
    if has_submaps:
        map_entry["side"] = request.form.get("side", map_entry.get("side", "")).strip()
    else:
        map_entry["side"] = ""
    save_app_state()
    if request.form.get("next") == "scrim":
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-comp", methods=["POST"])
def update_comp(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    use_section_sides = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), [])) or map_entry.get("map_name") in ATTACK_DEFENSE_MAPS
    section_count = int(request.form.get("section_count", "1"))
    sections = []
    for s in range(section_count):
        side_value = request.form.get(f"sec_{s}_side", "").strip() if use_section_sides else ""
        if side_value not in SIDES:
            side_value = ""
        sec = {
            "submap": request.form.get(f"sec_{s}_submap", "").strip(),
            "side": side_value,
            "score": build_score_text(
                request.form.get(f"sec_{s}_score_team1", "").strip(),
                request.form.get(f"sec_{s}_score_team2", "").strip(),
                request.form.get(f"sec_{s}_score", "").strip(),
            ),
            "team1": [],
            "team2": [],
        }
        for team in ("team1", "team2"):
            for i in range(6):
                hero = request.form.get(f"sec_{s}_{team}_hero_{i}", "").strip()
                player = request.form.get(f"sec_{s}_{team}_player_{i}", "").strip()
                sec[team].append({"hero": hero, "player": player})
        if team_has_duplicate_heroes(sec["team1"]) or team_has_duplicate_heroes(sec["team2"]):
            flash("Each team roster must use unique heroes in a section.", "error")
            return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))
        auto_assign_section_players_from_heroes(
            scrim,
            map_entry,
            sec,
            is_tournament=False,
        )
        sections.append(sec)
    map_entry["comp"] = sections
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/update-comp", methods=["POST"])
def update_tournament_comp(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    use_section_sides = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), [])) or map_entry.get("map_name") in ATTACK_DEFENSE_MAPS
    section_count = int(request.form.get("section_count", "1"))
    sections = []
    for s in range(section_count):
        side_value = request.form.get(f"sec_{s}_side", "").strip() if use_section_sides else ""
        if side_value not in SIDES:
            side_value = ""
        sec = {
            "submap": request.form.get(f"sec_{s}_submap", "").strip(),
            "side": side_value,
            "score": build_score_text(
                request.form.get(f"sec_{s}_score_team1", "").strip(),
                request.form.get(f"sec_{s}_score_team2", "").strip(),
                request.form.get(f"sec_{s}_score", "").strip(),
            ),
            "team1": [],
            "team2": [],
        }
        for team in ("team1", "team2"):
            for i in range(6):
                hero = request.form.get(f"sec_{s}_{team}_hero_{i}", "").strip()
                player = request.form.get(f"sec_{s}_{team}_player_{i}", "").strip()
                sec[team].append({"hero": hero, "player": player})
        if team_has_duplicate_heroes(sec["team1"]) or team_has_duplicate_heroes(sec["team2"]):
            flash("Each team roster must use unique heroes in a section.", "error")
            return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))
        auto_assign_section_players_from_heroes(
            tournament_match,
            map_entry,
            sec,
            is_tournament=True,
        )
        sections.append(sec)
    map_entry["comp"] = sections
    save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-comp-section/<int:section_index>", methods=["POST"])
def update_comp_section(scrim_id: int, map_id: int, section_index: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    sections = map_entry.get("comp", [])
    if section_index < 0 or section_index >= len(sections):
        abort(404)

    section = sections[section_index]

    section["submap"] = request.form.get("submap", section.get("submap", "")).strip()
    side_value = request.form.get("side", section.get("side", "")).strip()
    if side_value not in SIDES:
        side_value = ""
    section["side"] = side_value
    if "score_team1" in request.form or "score_team2" in request.form or "score" in request.form:
        section["score"] = build_score_text(
            request.form.get("score_team1", "").strip(),
            request.form.get("score_team2", "").strip(),
            request.form.get("score", section.get("score", "")).strip(),
        )
    
    # Store submap result if this section has a submap
    section_result = request.form.get("section_result", "").strip()
    if section_result in RESULTS:
        section["result"] = section_result
    elif section.get("submap"):
        inferred_submap_result = infer_result_from_score_text(section.get("score", ""), slot="team1")
        if inferred_submap_result in RESULTS:
            section["result"] = inferred_submap_result
        else:
            # Clear result if no valid result provided and score is not decisive.
            section.pop("result", None)

    for team in ("team1", "team2"):
        team_slots = []
        for i in range(6):
            hero = request.form.get(f"{team}_hero_{i}", "").strip()
            player = request.form.get(f"{team}_player_{i}", "").strip()
            team_slots.append({"hero": hero, "player": player})
        section[team] = team_slots

    if team_has_duplicate_heroes(section["team1"]) or team_has_duplicate_heroes(section["team2"]):
        flash("Each team roster must use unique heroes in a section.", "error")
        return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))

    auto_assign_section_players_from_heroes(
        scrim,
        map_entry,
        section,
        is_tournament=False,
    )

    map_entry["comp"][section_index] = section
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/update-comp-section/<int:section_index>", methods=["POST"])
def update_tournament_comp_section(tournament_id: int, map_id: int, section_index: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    sections = map_entry.get("comp", [])
    if section_index < 0 or section_index >= len(sections):
        abort(404)

    section = sections[section_index]
    section["submap"] = request.form.get("submap", section.get("submap", "")).strip()
    side_value = request.form.get("side", section.get("side", "")).strip()
    if side_value not in SIDES:
        side_value = ""
    section["side"] = side_value
    if "score_team1" in request.form or "score_team2" in request.form or "score" in request.form:
        section["score"] = build_score_text(
            request.form.get("score_team1", "").strip(),
            request.form.get("score_team2", "").strip(),
            request.form.get("score", section.get("score", "")).strip(),
        )
    
    # Store submap result if this section has a submap
    section_result = request.form.get("section_result", "").strip()
    if section_result in RESULTS:
        section["result"] = section_result
    elif section.get("submap"):
        inferred_submap_result = infer_result_from_score_text(section.get("score", ""), slot="team1")
        if inferred_submap_result in RESULTS:
            section["result"] = inferred_submap_result
        else:
            # Clear result if no valid result provided and score is not decisive.
            section.pop("result", None)

    for team in ("team1", "team2"):
        team_slots = []
        for i in range(6):
            hero = request.form.get(f"{team}_hero_{i}", "").strip()
            player = request.form.get(f"{team}_player_{i}", "").strip()
            team_slots.append({"hero": hero, "player": player})
        section[team] = team_slots

    if team_has_duplicate_heroes(section["team1"]) or team_has_duplicate_heroes(section["team2"]):
        flash("Each team roster must use unique heroes in a section.", "error")
        return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))

    auto_assign_section_players_from_heroes(
        tournament_match,
        map_entry,
        section,
        is_tournament=True,
    )

    map_entry["comp"][section_index] = section
    save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/add-comp-section", methods=["POST"])
def add_comp_section(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    sections = map_entry.setdefault("comp", [])
    if len(sections) < 4:
        sections.append({
            "submap": "",
            "side": "",
            "score": "",
            "team1": [{"hero": "", "player": ""} for _ in range(6)],
            "team2": [{"hero": "", "player": ""} for _ in range(6)],
        })
        save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/add-comp-section", methods=["POST"])
def add_tournament_match_comp_section(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    sections = map_entry.setdefault("comp", [])
    if len(sections) < 4:
        sections.append({
            "submap": "",
            "side": "",
            "score": "",
            "team1": [{"hero": "", "player": ""} for _ in range(6)],
            "team2": [{"hero": "", "player": ""} for _ in range(6)],
        })
        save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/add-comp-section", methods=["POST"])
def add_tournament_comp_section(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    sections = map_entry.setdefault("comp", [])
    if len(sections) < 4:
        sections.append({
            "submap": "",
            "side": "",
            "score": "",
            "team1": [{"hero": "", "player": ""} for _ in range(6)],
            "team2": [{"hero": "", "player": ""} for _ in range(6)],
        })
        save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/delete-event/<int:event_id>", methods=["POST"])
def delete_event(scrim_id: int, map_id: int, event_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    map_entry["events"] = [e for e in map_entry["events"] if e["id"] != event_id]
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/delete-event/<int:event_id>", methods=["POST"])
def delete_tournament_event(tournament_id: int, map_id: int, event_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    map_entry["events"] = [e for e in map_entry["events"] if e["id"] != event_id]
    save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/add-event", methods=["POST"])
def add_event_to_map(scrim_id: int, map_id: int):
    global NEXT_EVENT_ID

    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)

    timestamp = request.form.get("timestamp", "").strip()
    event_type = request.form.get("event_type", "").strip()
    description = request.form.get("description", "").strip()

    event_entry = {
        "id": NEXT_EVENT_ID,
        "timestamp": timestamp,
        "event_type": event_type,
        "description": description,
    }

    map_entry["events"].append(event_entry)
    NEXT_EVENT_ID += 1
    save_app_state()

    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/add-event", methods=["POST"])
def add_tournament_event_to_map(tournament_id: int, map_id: int):
    global NEXT_EVENT_ID

    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)

    event_entry = {
        "id": NEXT_EVENT_ID,
        "timestamp": request.form.get("timestamp", "").strip(),
        "event_type": request.form.get("event_type", "").strip(),
        "description": request.form.get("description", "").strip(),
    }

    map_entry["events"].append(event_entry)
    NEXT_EVENT_ID += 1
    save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/draft-simulator")
def draft_simulator():
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    return render_template(
        "draft_simulator.html",
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        teams=teams,
    )


try:
    init_db()
except Exception as e:
    app.logger.error(f"Failed to initialize database at startup: {type(e).__name__}: {e}")

try:
    load_app_state()
except Exception as e:
    app.logger.error(f"Failed to load app state at startup: {type(e).__name__}: {e}")

if (os.environ.get("RENDER") or "").strip().lower() == "true" and not is_persistent_db_configured():
    app.logger.warning(
        "Render persistent storage is not configured (DATABASE_PATH/RENDER_DISK_MOUNT_PATH missing). "
        "Data can be lost on redeploy. Current DB path: %s",
        DB_PATH,
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=True,
        use_reloader=True,
    )