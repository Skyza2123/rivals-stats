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
import shutil
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
from hero_analytics import (
    build_hero_usage_timeline,
    build_player_hero_map_breakdown,
    build_team_hero_profile,
)
from scrim_reporting import (
    build_atk_def_wr,
    build_pivot_wr,
    build_scrim_log_export_archive,
    build_scrim_log_rows,
    filter_scrim_log_rows,
)
from team_map_overview import build_team_map_overview
from team_detail_assembly import build_team_detail_matchup_context
from auth_helpers import (
    clear_auth_session as _clear_auth_session,
    configure_auth_helpers,
    current_auth_revision as _current_auth_revision,
    get_stored_password_hash as _get_stored_password_hash,
    get_stored_view_password_hash as _get_stored_view_password_hash,
    is_edit_session as _is_edit_session,
    is_password_configured as _is_password_configured,
    is_session_authenticated as _is_session_authenticated,
    is_write_request as _is_write_request,
    mark_session_authenticated as _mark_session_authenticated,
    normalize_next_path as _normalize_next_path,
    resolve_edit_password_secret as _resolve_edit_password_secret,
    resolve_view_password_secret as _resolve_view_password_secret,
)
from routes.teams import register_team_routes
from data import (
    HEROES, HERO_ROLES, HERO_TRANSFORMATIONS, MAPS, MAP_IMAGES, MAP_SUBMAPS,
    SIDES, RESULTS, EVENT_TYPES, ATTACK_DEFENSE_MAPS, MAP_MODES, MAP_TYPES,
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")


def static_asset_url(filename: str) -> str:
    """Return a static asset URL with mtime-based cache busting."""
    version = "1"
    try:
        asset_path = Path(app.static_folder) / filename
        if asset_path.exists():
            version = str(int(asset_path.stat().st_mtime))
    except OSError:
        pass
    return url_for("static", filename=filename, v=version)


@app.context_processor
def inject_static_asset_url() -> dict:
    return {"static_asset_url": static_asset_url}


@app.route("/favicon.ico")
def favicon_ico():
    return app.send_static_file("favicon.png")

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

    # If the database path is explicitly configured, keep logos next to it so
    # both data sets share the same persistence behavior across redeploys.
    configured_db = (os.environ.get("DATABASE_PATH") or "").strip()
    if configured_db and configured_db != ":memory:":
        return Path(configured_db).parent / "team_logos"

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
_LOGOS_ON_DISK = os.path.normcase(str(TEAM_LOGO_DIR.resolve())) != os.path.normcase(
    str((Path(app.static_folder) / "uploads" / "team_logos").resolve())
)
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
    "deadpoolstrat": "SupportPool",
    "suppool": "SupportPool",
    "deadpoolt": "Tankpool",
    "deadpooltank": "Tankpool",
    "deadpoolvanguard": "Tankpool",
    "dp": "DpsPool",
    "dpd": "DpsPool",
    "dps": "SupportPool",
    "dpspool": "DpsPool",
    "deadpoolduel": "DpsPool",
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
MAP_NAME_ALIASES = {
    "musume of contemplation": "Museum of Contemplation",
    "museum of contemplation": "Museum of Contemplation",
    "lower manhatten": "Lower Manhattan",
    "lower manhattan": "Lower Manhattan",
}


def _normalize_map_name_key(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def get_map_image_url(map_name: str) -> str:
    if not map_name:
        return ""
    direct_url = MAP_IMAGES.get(map_name)
    if direct_url:
        return direct_url
    canonical_name = MAP_NAME_ALIASES.get(_normalize_map_name_key(map_name), map_name)
    return MAP_IMAGES.get(canonical_name, "")

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
    "team2_protect1",
    "team1_ban2",
    "team1_protect1",
    "team1_ban3",
    "team2_ban2",
    "team1_protect2",
    "team2_ban3",
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
COMFORT_CORE_MIN_RATE = 40.0
RECENCY_HALFLIFE_DAYS = max(1.0, float(os.environ.get("MACHINE_RECENCY_HALFLIFE_DAYS", "14")))
MACHINE_TREND_MIN_POINTS = max(3, int(os.environ.get("MACHINE_TREND_MIN_POINTS", "3")))
MACHINE_HERO_TREND_BLEND = min(1.0, max(0.0, float(os.environ.get("MACHINE_HERO_TREND_BLEND", "0.80"))))
MACHINE_COMP_TREND_BLEND = min(1.0, max(0.0, float(os.environ.get("MACHINE_COMP_TREND_BLEND", "0.75"))))
MACHINE_HERO_TREND_CAP = max(0.0, float(os.environ.get("MACHINE_HERO_TREND_CAP", "16.0")))
MACHINE_COMP_TREND_CAP = max(0.0, float(os.environ.get("MACHINE_COMP_TREND_CAP", "14.0")))

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
        migrate_legacy_logo_paths_to_disk(conn)
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


register_team_routes(
    app,
    is_edit_session=_is_edit_session,
    get_db=get_db,
)


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

    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# Season midpoint dates used to infer recency for scrims that lack an explicit date.
# Keys are normalized season strings; dates are midpoints of the known season window.
_SEASON_MIDPOINT_DATES: dict[str, date] = {
    "6": date(2026, 1, 29),    # Season 6: Jan 16 – Feb 12
    "6.5": date(2026, 3, 1),   # Season 6.5: Feb 13 – Mar 19
    "7": date(2026, 4, 7),     # Season 7: Mar 20 – present (~Apr 24)
}


def _infer_date_from_season(season_raw: str) -> date | None:
    """Return the midpoint date for a known season string, or None if unrecognised."""
    key = normalize_season_value(season_raw).strip()
    return _SEASON_MIDPOINT_DATES.get(key)


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
    value = " ".join((raw_value or "").strip().split())
    if not value:
        return ""
    if value == UNSPECIFIED_SEASON_TOKEN:
        return UNSPECIFIED_SEASON_TOKEN

    lower_value = value.lower()
    if lower_value == "all":
        return "all"

    def _normalize_numeric_season_token(token: str) -> str:
        try:
            numeric = float(token)
        except (TypeError, ValueError):
            return token
        if numeric.is_integer():
            return str(int(numeric))
        return f"{numeric:.6f}".rstrip("0").rstrip(".")

    season_token_match = re.search(r"(?:^|\b)s(?:eason)?\s*([0-9]+(?:\.[0-9]+)?)\b", lower_value)
    if season_token_match:
        return _normalize_numeric_season_token(season_token_match.group(1))

    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", lower_value):
        return _normalize_numeric_season_token(lower_value)

    return value


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


def get_team_history_scrims(team_row: sqlite3.Row | dict) -> list[dict]:
    """Return scrim + tournament scrim history for a team row."""
    team_id = int(team_row["id"])
    team_name = (team_row["name"] or "").strip()
    scrims = get_scrims_for_team(team_id, team_name)
    tournament_scrims = build_team_tournament_scrims(team_row)
    return scrims + tournament_scrims


def get_team_history_for_sources(
    team_row: sqlite3.Row | dict,
    *,
    include_scrims: bool = True,
    include_tournaments: bool = True,
) -> list[dict]:
    """Return team history filtered by the machine source toggles."""
    team_id = int(team_row["id"])
    team_name = (team_row["name"] or "").strip()
    history: list[dict] = []
    if include_scrims:
        history.extend(get_scrims_for_team(team_id, team_name))
    if include_tournaments:
        history.extend(build_team_tournament_scrims(team_row))
    return history


def _bool_arg(name: str, default: bool = True) -> bool:
    raw_value = request.args.get(name)
    if raw_value is None:
        return default
    return str(raw_value).strip().lower() not in {"0", "false", "off", "no"}


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
    strict: bool = False,
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
    if strict:
        return selected
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

            player_hero_counts: Counter[str] = Counter()
            player_hero_first_seen: dict[str, int] = {}
            player_found = False
            for section_index, section in enumerate(map_entry.get("comp", [])):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(our_team_slot, []):
                    if (slot.get("player", "") or "").strip().lower() != target_name:
                        continue
                    player_found = True
                    h = _canonical_draft_hero(slot.get("hero", ""))
                    if h:
                        player_hero_counts[h] += 1
                        player_hero_first_seen.setdefault(h, section_index)

            if not player_found:
                continue
            player_heroes = set(player_hero_counts)

            for hero_h in all_heroes:
                if hero_h in enemy_bans:
                    times_banned[hero_h] += 1
                    if result == "Win":
                        ban_wins[hero_h] += 1
                    elif result == "Loss":
                        ban_losses[hero_h] += 1
                    pivot_candidates = [
                        (h, count)
                        for h, count in player_hero_counts.items()
                        if h != hero_h
                    ]
                    if pivot_candidates:
                        pivot_candidates.sort(
                            key=lambda item: (-item[1], player_hero_first_seen.get(item[0], 9999), item[0])
                        )
                        pivot_hero = pivot_candidates[0][0]
                        pivot_stats[hero_h][pivot_hero]["count"] += 1
                        if result == "Win":
                            pivot_stats[hero_h][pivot_hero]["wins"] += 1
                        elif result == "Loss":
                            pivot_stats[hero_h][pivot_hero]["losses"] += 1
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
    if re.match(r"^(?:r|ringer|sub|substitute|stand[\s-]?in|merc(?:enary)?)\s*(?::|\s*-\s+)", lowered):
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
    name = re.sub(r"^(?:r|ringer|sub|substitute|stand[\s-]?in|merc(?:enary)?)\s*(?::\s*|\s*-\s+)", "", name, flags=re.IGNORECASE)
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
    # Fuzzy fallback using the same match-key logic as the importer
    for team in tournament_teams:
        if isinstance(team, dict) and _team_names_match(team_name, team.get("name", "")):
            return team
    return None


def _resolve_team_from_db(raw_name: str) -> tuple[str, list[str]]:
    """Return (canonical_name, player_list) from the DB for raw_name.
    Falls back to (raw_name, []) if no match is found."""
    db = get_db()
    all_teams = db.execute("SELECT id, name FROM teams ORDER BY id").fetchall()
    for row in all_teams:
        if _team_names_match(raw_name, row["name"]):
            canonical = row["name"]
            players = [
                p["name"]
                for p in db.execute(
                    "SELECT name FROM players WHERE team_id = ? AND COALESCE(is_sub, 0) = 0 ORDER BY name COLLATE NOCASE",
                    (row["id"],),
                ).fetchall()
            ]
            return canonical, players
    return (raw_name or "").strip(), []


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


def _blank_import_draft() -> dict:
    return {
        "team1": {"ban1": "", "protect1": "", "ban2": "", "ban3": "", "ban4": "", "protect2": ""},
        "team2": {"ban1": "", "protect1": "", "ban2": "", "ban3": "", "ban4": "", "protect2": ""},
    }


def _blank_import_comp_section() -> dict:
    return {
        "submap": "",
        "side": "",
        "score": "",
        "team1": [{"hero": "", "player": ""} for _ in range(6)],
        "team2": [{"hero": "", "player": ""} for _ in range(6)],
    }


def _normalize_import_hero(raw_hero: str | None) -> str:
    hero_text = (raw_hero or "").strip()
    if not hero_text or hero_text.lower() == "skipped":
        return ""

    # Imported comp cells can include swaps. The match page stores one hero per
    # player slot, so keep the opening pick as the displayed comp hero.
    hero_text = hero_text.split("/")[0].strip()
    compact = _compact_text(hero_text)
    if compact in {"deadpoolvan", "deadpoolv", "deadpoolvanguard"}:
        return "Tankpool"
    if compact in {"deadpooldps", "deadpoolduelist", "deadpoolduel"}:
        return "DpsPool"
    if compact in {"deadpoolsup", "deadpoolsupp", "deadpoolsupport", "deadpoolstrategist", "deadpoolstrat"} or compact == "suppool":
        return "SupportPool"
    return normalize_hero_slot_value(hero_text)


def _tournament_import_score_match(line: str) -> re.Match | None:
    return re.match(r"^(.+?)\s+(\d+)\s*-\s*(\d+)\s+(.+)$", (line or "").strip())


def _map_import_team_label(label: str, alias_map: dict[str, str]) -> str:
    cleaned = (label or "").strip()
    return alias_map.get(cleaned) or alias_map.get(cleaned.lower()) or alias_map.get(_compact_text(cleaned)) or cleaned


def _set_import_alias(alias_map: dict[str, str], label: str, value: str) -> None:
    cleaned = (label or "").strip()
    if not cleaned:
        return
    alias_map[cleaned] = value
    alias_map[cleaned.lower()] = value
    compact = _compact_text(cleaned)
    if compact:
        alias_map[compact] = value


def _lookup_import_label(mapping: dict[str, str], label: str) -> str:
    cleaned = (label or "").strip()
    return mapping.get(cleaned) or mapping.get(cleaned.lower()) or mapping.get(_compact_text(cleaned)) or ""


def _resolve_import_map_team_labels(left_label: str, right_label: str, team1_name: str, team2_name: str) -> tuple[str, str]:
    left_clean = (left_label or "").strip()
    right_clean = (right_label or "").strip()
    if _team_names_match(left_clean, team1_name) and _team_names_match(right_clean, team2_name):
        return team1_name, team2_name
    if _team_names_match(left_clean, team2_name) and _team_names_match(right_clean, team1_name):
        return team2_name, team1_name
    # Partial match: if only one label is recognisable (e.g. short abbreviation),
    # infer the other team rather than defaulting to left=team1.
    if _team_names_match(left_clean, team1_name):
        return team1_name, team2_name
    if _team_names_match(left_clean, team2_name):
        return team2_name, team1_name
    if _team_names_match(right_clean, team2_name):
        return team1_name, team2_name
    if _team_names_match(right_clean, team1_name):
        return team2_name, team1_name
    return team1_name, team2_name


def _assign_import_draft_row(draft: dict, side: str, headers: list[str], values: list[str]) -> None:
    if side not in TEAM_SLOTS:
        return

    ban_index = 1
    protect_index = 1
    for header, raw_value in zip(headers, values):
        hero_name = _normalize_import_hero(raw_value)
        if not hero_name:
            continue

        header_value = (header or "").strip().lower()
        if header_value.startswith("ban") and ban_index <= 4:
            draft[side][f"ban{ban_index}"] = hero_name
            ban_index += 1
        elif header_value.startswith("protect") and protect_index <= 2:
            draft[side][f"protect{protect_index}"] = hero_name
            protect_index += 1


def _split_import_table_row(line: str) -> list[str]:
    if "\t" in (line or ""):
        return [cell.strip() for cell in line.split("\t")]
    return [cell.strip() for cell in re.split(r"\s{2,}", (line or "").strip()) if cell.strip()]


def _parse_import_draft(block_lines: list[str], side_by_label: dict[str, str]) -> dict:
    draft = _blank_import_draft()
    for index, line in enumerate(block_lines):
        cells = _split_import_table_row(line)
        if not cells or cells[0].lower() != "ban":
            continue

        headers = cells
        for row_offset, row in enumerate(block_lines[index + 1:index + 3]):
            row_cells = _split_import_table_row(row)
            if len(row_cells) < 2:
                continue
            side = _lookup_import_label(side_by_label, row_cells[0])
            if side not in TEAM_SLOTS:
                side = "team1" if row_offset == 0 else "team2"
            _assign_import_draft_row(draft, side, headers, row_cells[1:])
        break
    return draft


def _parse_import_comp(block_lines: list[str], side_by_label: dict[str, str]) -> dict:
    section = _blank_import_comp_section()
    for index, line in enumerate(block_lines):
        cells = _split_import_table_row(line)
        hero_columns = [cell_index for cell_index, cell in enumerate(cells) if cell.lower() == "hero"]
        if len(cells) < 4 or len(hero_columns) < 2 or hero_columns[0] != 1:
            continue

        right_hero_index = hero_columns[-1]
        left_side = _lookup_import_label(side_by_label, cells[0])
        right_side = _lookup_import_label(side_by_label, cells[-1])
        if left_side not in TEAM_SLOTS:
            left_side = "team1"
        if right_side not in TEAM_SLOTS:
            right_side = "team2"

        for row in block_lines[index + 1:index + 7]:
            row_cells = _split_import_table_row(row)
            if len(row_cells) <= right_hero_index or len(row_cells) < 4:
                continue

            left_slot = {"player": row_cells[0], "hero": _normalize_import_hero(row_cells[1])}
            right_slot = {"player": row_cells[-1], "hero": _normalize_import_hero(row_cells[right_hero_index])}

            if len(section[left_side]) < 6:
                section[left_side].append(left_slot)
            else:
                empty_index = next((i for i, slot in enumerate(section[left_side]) if not slot.get("hero") and not slot.get("player")), None)
                if empty_index is not None:
                    section[left_side][empty_index] = left_slot

            if len(section[right_side]) < 6:
                section[right_side].append(right_slot)
            else:
                empty_index = next((i for i, slot in enumerate(section[right_side]) if not slot.get("hero") and not slot.get("player")), None)
                if empty_index is not None:
                    section[right_side][empty_index] = right_slot
        break

    return section


def parse_tournament_match_text_import(raw_text: str) -> dict:
    lines = [(line or "").rstrip("\r") for line in (raw_text or "").splitlines()]
    non_empty_lines = [line.strip() for line in lines if line.strip()]
    match_line = ""
    match = None
    for line in non_empty_lines:
        candidate = _tournament_import_score_match(line)
        if candidate:
            match_line = line
            match = candidate
            break

    if match is None:
        raise ValueError("Could not find a match score line like Team A 3-2 Team B.")

    team1_name = match.group(1).strip()
    team2_name = match.group(4).strip()
    match_score = f"{match.group(2)} - {match.group(3)}"
    notes = non_empty_lines[0] if non_empty_lines and non_empty_lines[0] != match_line else ""

    map_start_indices = [index for index, line in enumerate(lines) if re.match(r"^\s*MAP\s+\d+\s+-", line, re.IGNORECASE)]
    if not map_start_indices:
        raise ValueError("Could not find any MAP sections in the uploaded file.")

    first_score_match = None
    for line in lines[map_start_indices[0] + 1:]:
        first_score_match = _tournament_import_score_match(line)
        if first_score_match:
            break
    alias_map: dict[str, str] = {}
    if first_score_match:
        first_left = first_score_match.group(1).strip()
        first_right = first_score_match.group(4).strip()
        first_left_team, first_right_team = _resolve_import_map_team_labels(first_left, first_right, team1_name, team2_name)
        _set_import_alias(alias_map, first_left, first_left_team)
        _set_import_alias(alias_map, first_right, first_right_team)

    maps: list[dict] = []
    players_by_team: dict[str, set[str]] = {team1_name: set(), team2_name: set()}
    for position, start_index in enumerate(map_start_indices):
        end_index = map_start_indices[position + 1] if position + 1 < len(map_start_indices) else len(lines)
        block_lines = lines[start_index:end_index]
        header = block_lines[0].strip()
        header_match = re.match(r"^MAP\s+\d+\s+-\s*([^:]+):\s*(.+)$", header, re.IGNORECASE)
        map_name = _match_map_name(header_match.group(2).strip() if header_match else header)

        score_match = next((_tournament_import_score_match(line) for line in block_lines[1:] if _tournament_import_score_match(line)), None)
        if score_match:
            left_label = score_match.group(1).strip()
            right_label = score_match.group(4).strip()
            left_score = score_match.group(2)
            right_score = score_match.group(3)
        else:
            left_label = team1_name
            right_label = team2_name
            left_score = ""
            right_score = ""

        replay_line = next(
            (line.strip() for line in block_lines if re.match(r"^\s*Replay\s+ID\s*:", line, re.IGNORECASE)),
            "",
        )
        left_team_name = _map_import_team_label(left_label, alias_map)
        right_team_name = _map_import_team_label(right_label, alias_map)
        side_by_label: dict[str, str] = {}
        for label in (left_label, left_team_name):
            _set_import_alias(side_by_label, label, "team1")
        for label in (right_label, right_team_name):
            _set_import_alias(side_by_label, label, "team2")

        draft = _parse_import_draft(block_lines, side_by_label)
        comp_section = _parse_import_comp(block_lines, side_by_label)
        for side, team_name in (("team1", left_team_name), ("team2", right_team_name)):
            players_by_team.setdefault(team_name, set())
            for slot in comp_section.get(side, []):
                player_name = (slot.get("player") or "").strip()
                if player_name:
                    players_by_team[team_name].add(player_name)

        score = f"{left_score} - {right_score}" if left_score and right_score else ""
        our_team_slot = "team1" if _team_names_match(left_team_name, team1_name) else "team2"
        maps.append({
            "map_name": map_name,
            "map_type": DEFAULT_MAP_TYPE,
            "side": "",
            "our_team_slot": our_team_slot,
            "result": infer_result_from_score_text(score, slot=our_team_slot) if score else "",
            "score": score,
            "team1_name": left_team_name,
            "team2_name": right_team_name,
            "draft": draft,
            "comp": [comp_section],
            "notes": replay_line,
            "vod_url": "",
            "events": [],
        })

    return {
        "team1_name": team1_name,
        "team2_name": team2_name,
        "match_score": match_score,
        "notes": notes,
        "maps": maps,
        "players_by_team": {team_name: sorted(players) for team_name, players in players_by_team.items()},
    }


def find_or_add_tournament_team_from_import(tournament_record: dict, team_name: str, players: list[str] | None = None) -> dict:
    # Resolve canonical name + roster from the DB first
    db_canonical, db_players = _resolve_team_from_db(team_name)
    # Merge imported players with any DB roster entries
    merged_players = list(db_players)
    for p in (players or []):
        if p and p not in merged_players:
            merged_players.append(p)

    # Match against an existing tournament team using the canonical name
    existing_team = find_tournament_team_by_name(tournament_record.get("tournament_teams", []), db_canonical)
    if existing_team is not None:
        existing_players = existing_team.setdefault("players", [])
        for player_name in merged_players:
            if player_name and player_name not in existing_players:
                existing_players.append(player_name)
        return existing_team

    new_team = {
        "id": next_tournament_team_id(tournament_record),
        "name": db_canonical or "Unknown Team",
        "players": [p for p in merged_players if p],
    }
    tournament_record.setdefault("tournament_teams", []).append(new_team)
    return new_team


def get_tournament_map_loser_team_id(map_entry: dict) -> int | None:
    left_score, right_score = split_score_pair(map_entry.get("score", ""))
    if left_score.isdigit() and right_score.isdigit():
        left_value = int(left_score)
        right_value = int(right_score)
        if left_value > right_value:
            return map_entry.get("team2_tournament_team_id")
        if right_value > left_value:
            return map_entry.get("team1_tournament_team_id")

    result = str(map_entry.get("result", "")).strip()
    our_team_slot = map_entry.get("our_team_slot", "team1")
    if our_team_slot not in TEAM_SLOTS:
        our_team_slot = "team1"
    other_slot = "team2" if our_team_slot == "team1" else "team1"
    if result == "Win":
        return map_entry.get(f"{other_slot}_tournament_team_id")
    if result == "Loss":
        return map_entry.get(f"{our_team_slot}_tournament_team_id")
    return None


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
                "image": get_map_image_url(map_name),
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
                "image": get_map_image_url(map_name),
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
        "map_types": MAP_TYPES,
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


def migrate_legacy_logo_paths_to_disk(conn: sqlite3.Connection) -> None:
    """Move legacy static logo references to persistent disk-backed paths."""
    if not _LOGOS_ON_DISK:
        return

    static_logo_root = (Path(app.static_folder) / "uploads" / "team_logos").resolve()
    TEAM_LOGO_DIR.mkdir(parents=True, exist_ok=True)

    for table_name in ("teams", "enemy_teams"):
        rows = conn.execute(
            f"SELECT id, logo_path FROM {table_name} WHERE logo_path LIKE ?",
            ("uploads/team_logos/%",),
        ).fetchall()
        for row in rows:
            stored_path = (row["logo_path"] or "").strip()
            if not stored_path:
                continue

            source = (Path(app.static_folder) / stored_path).resolve()
            try:
                source.relative_to(static_logo_root)
            except ValueError:
                continue
            if not source.exists() or not source.is_file():
                continue

            source_name = secure_filename(source.name)
            if not source_name:
                continue

            destination = TEAM_LOGO_DIR / source_name
            if destination.exists():
                stem = Path(source_name).stem or "team-logo"
                suffix = Path(source_name).suffix.lower()
                destination = TEAM_LOGO_DIR / f"{stem}-{uuid4().hex[:8]}{suffix}"

            try:
                shutil.copy2(source, destination)
            except OSError:
                continue

            conn.execute(
                f"UPDATE {table_name} SET logo_path = ? WHERE id = ?",
                (f"__disk__/{destination.name}", row["id"]),
            )


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
                "map_image": get_map_image_url(map_name),
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

            # Build map: roster player -> section-counted heroes they played this map.
            # A ban-pivot row should count one replacement per map, not every
            # short section swap the player touched after their main was banned.
            player_heroes_this_map: dict[str, Counter[str]] = defaultdict(Counter)
            player_hero_first_seen_this_map: dict[str, dict[str, int]] = defaultdict(dict)
            for section_index, section in enumerate(map_entry.get("comp", [])):
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
                            player_heroes_this_map[roster_pname][h] += 1
                            player_hero_first_seen_this_map[roster_pname].setdefault(h, section_index)

            for pname, mains in player_main_heroes.items():
                for main_h in mains:
                    if main_h in enemy_bans:
                        player_main_ban_total[(pname, main_h)] += 1
                        pivot_candidates = [
                            (hero_name, count)
                            for hero_name, count in player_heroes_this_map.get(pname, {}).items()
                            if hero_name != main_h
                        ]
                        if not pivot_candidates:
                            continue
                        first_seen = player_hero_first_seen_this_map.get(pname, {})
                        pivot_candidates.sort(
                            key=lambda item: (-item[1], first_seen.get(item[0], 9999), item[0])
                        )
                        pivot_hero = pivot_candidates[0][0]
                        player_ban_pivot_counts[(pname, main_h)][pivot_hero]["count"] += 1
                        if pivot_result == "Win":
                            player_ban_pivot_counts[(pname, main_h)][pivot_hero]["wins"] += 1
                        elif pivot_result == "Loss":
                            player_ban_pivot_counts[(pname, main_h)][pivot_hero]["losses"] += 1

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


def _predictor_map_values(map_entry: dict) -> dict[str, str]:
    map_values: dict[str, str] = {}
    draft = map_entry.get("draft", {})
    if not isinstance(draft, dict):
        return {field_key: "" for field_key in PREDICTOR_INPUT_ORDER}

    for group in PREDICTOR_GROUPS:
        for team_slot, slot_key, field_key in group:
            team_draft = draft.get(team_slot, {})
            if not isinstance(team_draft, dict):
                team_draft = {}
            map_values[field_key] = _canonical_draft_hero(team_draft.get(slot_key, ""))
    return map_values


def _predictor_richest_comp(map_entry: dict, team_slot: str) -> tuple[str, ...]:
    comp_sections = map_entry.get("comp", [])
    if not isinstance(comp_sections, list):
        return ()

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

    return tuple(richest_comp)


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
            "exact_matching_maps": 0,
            "training_maps": 0,
            "targets": [],
            "status": "complete",
        }

    exact_matching_maps = 0
    training_maps = 0
    exact_target_counts = {
        field_key: defaultdict(int)
        for _, _, field_key in next_targets
    }
    slot_prior_counts = {
        field_key: defaultdict(int)
        for _, _, field_key in next_targets
    }
    source_target_counts = {
        field_key: {
            source_field: defaultdict(lambda: defaultdict(int))
            for source_field in PREDICTOR_INPUT_ORDER
        }
        for _, _, field_key in next_targets
    }
    source_value_vocab = {
        field_key: {
            source_field: set()
            for source_field in PREDICTOR_INPUT_ORDER
        }
        for _, _, field_key in next_targets
    }
    exact_comp_counts = {
        "team1": defaultdict(int),
        "team2": defaultdict(int),
    }
    comp_prior_counts = {
        "team1": defaultdict(int),
        "team2": defaultdict(int),
    }
    filled_inputs = {
        field_key: hero_name
        for field_key, hero_name in normalized_inputs.items()
        if hero_name
    }
    used_heroes = {hero_name for hero_name in normalized_inputs.values() if hero_name}

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            map_values = _predictor_map_values(map_entry)
            if not any(map_values.values()):
                continue
            training_maps += 1

            for _, _, field_key in next_targets:
                hero = map_values.get(field_key, "")
                if hero:
                    slot_prior_counts[field_key][hero] += 1
                    for source_field, source_hero in filled_inputs.items():
                        if map_values.get(source_field, "") == source_hero:
                            source_target_counts[field_key][source_field][source_hero][hero] += 1
                        observed_source_hero = map_values.get(source_field, "")
                        if observed_source_hero:
                            source_value_vocab[field_key][source_field].add(observed_source_hero)

            for team_slot in TEAM_SLOTS:
                richest_comp = _predictor_richest_comp(map_entry, team_slot)
                if richest_comp:
                    comp_prior_counts[team_slot][richest_comp] += 1

            if any(
                normalized_inputs[field_key] and map_values.get(field_key, "") != normalized_inputs[field_key]
                for field_key in PREDICTOR_INPUT_ORDER
            ):
                continue

            exact_matching_maps += 1
            for _, _, field_key in next_targets:
                hero = map_values.get(field_key, "")
                if hero:
                    exact_target_counts[field_key][hero] += 1

            for team_slot in TEAM_SLOTS:
                richest_comp = _predictor_richest_comp(map_entry, team_slot)
                if richest_comp:
                    exact_comp_counts[team_slot][richest_comp] += 1

    target_rows = []
    for team_slot, slot_key, field_key in next_targets:
        exact_total = sum(exact_target_counts[field_key].values())
        support_by_hero: dict[str, int] = defaultdict(int)
        option_rows: list[dict] = []

        if exact_total:
            options = sorted(exact_target_counts[field_key].items(), key=lambda item: item[1], reverse=True)
            option_rows = [
                {
                    "hero": hero,
                    "count": count,
                    "rate": round((count / exact_total) * 100, 1) if exact_total else 0,
                }
                for hero, count in options
                if hero not in used_heroes
            ]
        else:
            slot_counts = slot_prior_counts[field_key]
            candidate_heroes = [hero for hero in slot_counts.keys() if hero not in used_heroes]
            slot_total = sum(slot_counts.values())
            alpha = 1.0
            scored_candidates: list[tuple[str, float]] = []

            if candidate_heroes and slot_total:
                vocab_size = max(len(candidate_heroes), 1)
                for hero in candidate_heroes:
                    hero_count = slot_counts.get(hero, 0)
                    support_by_hero[hero] = hero_count
                    # Empirical prior for this slot.
                    log_score = math.log((hero_count + alpha) / (slot_total + alpha * vocab_size))

                    # Naive Bayes context likelihood from observed draft history only.
                    for source_field, source_hero in filled_inputs.items():
                        source_counts_for_value = source_target_counts[field_key][source_field].get(source_hero, {})
                        joint_count = source_counts_for_value.get(hero, 0)
                        source_vocab_size = max(len(source_value_vocab[field_key][source_field]), 1)
                        log_score += math.log((joint_count + alpha) / (hero_count + alpha * (source_vocab_size + 1)))

                    scored_candidates.append((hero, log_score))

                if scored_candidates:
                    max_log_score = max(score for _, score in scored_candidates)
                    normalized = [
                        (hero, math.exp(score - max_log_score))
                        for hero, score in scored_candidates
                    ]
                    total_weight = sum(weight for _, weight in normalized)
                    option_rows = [
                        {
                            "hero": hero,
                            "count": support_by_hero.get(hero, 0),
                            "rate": round((weight / total_weight) * 100, 1) if total_weight else 0,
                        }
                        for hero, weight in sorted(normalized, key=lambda item: item[1], reverse=True)
                    ]

        target_rows.append(
            {
                "field_key": field_key,
                "team_label": "Team 1" if team_slot == "team1" else "Team 2",
                "slot_label": _draft_slot_label(slot_key),
                "options": option_rows[:8],
            }
        )

    likely_comps = []
    for team_slot in TEAM_SLOTS:
        comp_source = exact_comp_counts[team_slot] if exact_comp_counts[team_slot] else comp_prior_counts[team_slot]
        comp_total = sum(comp_source.values())
        comp_options = sorted(comp_source.items(), key=lambda item: item[1], reverse=True)
        if not comp_options:
            continue

        top_comp, top_count = comp_options[0]
        likely_comps.append(
            {
                "team_key": team_slot,
                "team_label": "Team 1" if team_slot == "team1" else "Team 2",
                "heroes": list(top_comp),
                "count": top_count,
                "rate": round((top_count / comp_total) * 100, 1) if comp_total else 0,
            }
        )

    return {
        "inputs": cleaned_inputs,
        "matching_maps": exact_matching_maps,
        "exact_matching_maps": exact_matching_maps,
        "training_maps": training_maps,
        "targets": target_rows,
        "likely_comps": likely_comps,
        "status": "ready" if training_maps else "empty",
    }


def build_opponent_tree_model(team_scrims: list[dict], hero_pool_scrims: list[dict] | None = None) -> dict:
    if hero_pool_scrims is None:
        hero_pool_scrims = team_scrims

    if not team_scrims and not hero_pool_scrims:
        return {
            "status": "empty",
            "training_maps": 0,
            "comfort_core_rows": [],
            "hero_pool_rows": [],
            "ban_line_rows": [],
            "comp_rows": [],
            "comp_path_rows": [],
            "volatile_hero_rows": [],
            "pivot_rows": [],
            "mode_hero_rows": [],
            "mode_comp_rows": [],
            "equivalent_path_rows": [],
            "map_state_rows": [],
        }

    def map_type_weight(raw_map_type: str) -> float:
        normalized = normalize_map_type_value(raw_map_type)
        if normalized == "PTW":
            return 1.75
        if normalized == "Test":
            return 0.55
        return 1.0

    def state_label(required_modes: set[str], *, is_first_map: bool) -> str:
        if is_first_map:
            return "Opening Control"
        if not required_modes:
            return "Cycle Reset"
        ordered = [mode for mode in ("Control", "Escort", "Hybrid") if mode in required_modes]
        if len(ordered) == 1:
            return f"Need {ordered[0]}"
        if len(ordered) == 2:
            return f"Need {' or '.join(ordered)}"
        return "Open Cycle"

    def legal_modes_for_next_map(index: int, cycle_modes_played: set[str]) -> set[str]:
        if index == 0:
            return {"Control"}
        if len(cycle_modes_played) == 3:
            cycle_modes_played = set()
        return {"Control", "Escort", "Hybrid"} - cycle_modes_played

    def split_line(line_values: tuple[str, ...] | list[str]) -> dict[str, list[str]]:
        values = list(line_values)
        return {
            "bans": [values[i] for i in (0, 2, 3, 5) if i < len(values) and values[i]],
            "protects": [values[i] for i in (1, 4) if i < len(values) and values[i]],
        }

    def line_distance(a: tuple[str, ...] | list[str], b: tuple[str, ...] | list[str]) -> int:
        a_values = list(a)
        b_values = list(b)
        return sum(1 for idx in range(min(len(a_values), len(b_values))) if a_values[idx] != b_values[idx])

    def weighted_linear_delta_pct(points: list[tuple[float, float, float]]) -> float:
        if len(points) < MACHINE_TREND_MIN_POINTS:
            return 0.0
        valid_points = [(x, y, w) for x, y, w in points if w > 0]
        if len(valid_points) < MACHINE_TREND_MIN_POINTS:
            return 0.0

        min_x = min(x for x, _y, _w in valid_points)
        max_x = max(x for x, _y, _w in valid_points)
        x_range = max_x - min_x
        if x_range <= 0:
            return 0.0

        sum_w = sum(w for _x, _y, w in valid_points)
        if sum_w <= 0:
            return 0.0

        mean_x = sum(x * w for x, _y, w in valid_points) / sum_w
        mean_y = sum(y * w for _x, y, w in valid_points) / sum_w
        cov_xy = sum(w * (x - mean_x) * (y - mean_y) for x, y, w in valid_points)
        var_x = sum(w * (x - mean_x) ** 2 for x, _y, w in valid_points)
        if var_x <= 1e-9:
            return 0.0

        slope = cov_xy / var_x
        return slope * x_range * 100.0

    analytics = build_scrim_analytics(team_scrims)
    total_maps = analytics.get("summary", {}).get("total_maps", 0)
    weighted_total_maps = 0.0
    weighted_total_wins = 0.0
    recency_decay_lambda = math.log(2.0) / RECENCY_HALFLIFE_DAYS
    dated_scrim_dates = [
        parsed
        for parsed in (
            _parse_scrim_date(scrim.get("scrim_date", ""))
            or _infer_date_from_season(scrim.get("season", ""))
            for scrim in team_scrims
        )
        if parsed is not None
    ]
    newest_scrim_date = max(dated_scrim_dates) if dated_scrim_dates else None
    earliest_scrim_date = min(dated_scrim_dates) if dated_scrim_dates else None

    hero_weighted_apps = defaultdict(float)
    hero_weighted_wins = defaultdict(float)
    hero_weighted_players = defaultdict(set)
    hero_player_weighted_apps: dict[str, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
    hero_player_weighted_wins: dict[str, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
    line_counts: defaultdict[tuple[str, ...], float] = defaultdict(float)
    line_wins: defaultdict[tuple[str, ...], float] = defaultdict(float)
    comp_counts: defaultdict[tuple[str, ...], float] = defaultdict(float)
    comp_wins: defaultdict[tuple[str, ...], float] = defaultdict(float)
    line_to_comp_counts: dict[tuple[str, ...], defaultdict[tuple[str, ...], float]] = defaultdict(lambda: defaultdict(float))
    comp_to_line_counts: dict[tuple[str, ...], defaultdict[tuple[str, ...], float]] = defaultdict(lambda: defaultdict(float))
    hero_presence_maps: defaultdict[str, float] = defaultdict(float)
    hero_presence_wins: defaultdict[str, float] = defaultdict(float)
    mode_hero_counts: dict[str, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
    mode_comp_counts: dict[str, defaultdict[tuple[str, ...], float]] = defaultdict(lambda: defaultdict(float))
    mode_totals: defaultdict[str, float] = defaultdict(float)
    mode_comp_totals: defaultdict[str, float] = defaultdict(float)
    map_state_counts: dict[str, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
    map_state_totals: defaultdict[str, float] = defaultdict(float)
    hero_trend_points: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    comp_trend_points: dict[tuple[str, ...], list[tuple[float, float, float]]] = defaultdict(list)
    map_time_index = 0
    undated_offset = (newest_scrim_date - earliest_scrim_date).days + 1 if newest_scrim_date and earliest_scrim_date else 0

    def scrim_sort_key(item: tuple[int, dict]) -> tuple[bool, date, int]:
        idx, scrim = item
        scrim_date = (
            _parse_scrim_date(scrim.get("scrim_date", ""))
            or _infer_date_from_season(scrim.get("season", ""))
        )
        return (scrim_date is None, scrim_date or date.min, idx)

    for _scrim_idx, scrim in sorted(enumerate(team_scrims), key=scrim_sort_key):
        scrim_date = (
            _parse_scrim_date(scrim.get("scrim_date", ""))
            or _infer_date_from_season(scrim.get("season", ""))
        )
        recency_weight = 1.0
        if newest_scrim_date is not None and scrim_date is not None:
            age_days = max(0, (newest_scrim_date - scrim_date).days)
            recency_weight = math.exp(-recency_decay_lambda * age_days)

        used_maps_in_series: set[str] = set()
        cycle_modes_played: set[str] = set()
        ordered_maps = [map_entry for map_entry in scrim.get("maps", []) if isinstance(map_entry, dict)]

        for index, map_entry in enumerate(ordered_maps):
            map_time_index += 1
            if scrim_date is not None and earliest_scrim_date is not None:
                time_x = float((scrim_date - earliest_scrim_date).days)
            else:
                time_x = float(undated_offset + map_time_index)
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            outcome = get_map_outcome_for_slot(map_entry, our_team_slot)
            decided_outcome = 1.0 if outcome == "Win" else 0.0 if outcome == "Loss" else None
            map_weight = map_type_weight(map_entry.get("map_type", "")) * recency_weight
            weighted_total_maps += map_weight
            if outcome == "Win":
                weighted_total_wins += map_weight

            map_seen_heroes: set[str] = set()
            map_seen_hero_players: set[tuple[str, str]] = set()
            draft = map_entry.get("draft", {})
            our_draft = draft.get(our_team_slot, {}) if isinstance(draft, dict) else {}
            if not isinstance(our_draft, dict):
                our_draft = {}

            line_values: list[str] = []
            line_is_complete = True
            for slot_key in DRAFT_SLOT_ORDER:
                hero_name = _canonical_draft_hero(our_draft.get(slot_key, ""))
                if hero_name:
                    line_values.append(hero_name)
                else:
                    line_values.append("")
                    line_is_complete = False

            if line_is_complete:
                line_key = tuple(line_values)
                line_counts[line_key] += map_weight
                if outcome == "Win":
                    line_wins[line_key] += map_weight

            richest_comp = _predictor_richest_comp(map_entry, our_team_slot)
            if richest_comp:
                comp_counts[richest_comp] += map_weight
                if outcome == "Win":
                    comp_wins[richest_comp] += map_weight
                if decided_outcome is not None:
                    comp_trend_points[richest_comp].append((time_x, decided_outcome, map_weight))
                if line_is_complete:
                    line_key = tuple(line_values)
                    line_to_comp_counts[line_key][richest_comp] += map_weight
                    comp_to_line_counts[richest_comp][line_key] += map_weight
                for hero_name in set(richest_comp):
                    hero_presence_maps[hero_name] += map_weight
                    if outcome == "Win":
                        hero_presence_wins[hero_name] += map_weight

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
                    if hero_name not in map_seen_heroes:
                        hero_weighted_apps[hero_name] += map_weight
                        if outcome == "Win":
                            hero_weighted_wins[hero_name] += map_weight
                        map_seen_heroes.add(hero_name)
                    if player_name:
                        hero_weighted_players[hero_name].add(player_name)
                        hero_player_key = (hero_name, player_name)
                        if hero_player_key not in map_seen_hero_players:
                            hero_player_weighted_apps[hero_name][player_name] += map_weight
                            if outcome == "Win":
                                hero_player_weighted_wins[hero_name][player_name] += map_weight
                            map_seen_hero_players.add(hero_player_key)

            if decided_outcome is not None:
                for hero_name in map_seen_heroes:
                    hero_trend_points[hero_name].append((time_x, decided_outcome, map_weight))

            map_name = (map_entry.get("map_name", "") or "").strip()
            mode_name = MAP_MODES.get(map_name, "Other")
            if mode_name in {"Control", "Escort", "Hybrid"}:
                mode_totals[mode_name] += map_weight
                if richest_comp:
                    mode_comp_counts[mode_name][richest_comp] += map_weight
                    mode_comp_totals[mode_name] += map_weight
                    for hero_name in set(richest_comp):
                        mode_hero_counts[mode_name][hero_name] += map_weight
            legal_modes = legal_modes_for_next_map(index, cycle_modes_played)
            legal_maps = [
                candidate
                for candidate, candidate_mode in MAP_MODES.items()
                if candidate_mode in legal_modes and candidate not in used_maps_in_series
            ]
            label = state_label(legal_modes, is_first_map=(index == 0))
            if map_name and map_name in legal_maps:
                map_state_counts[label][map_name] += map_weight
                map_state_totals[label] += map_weight

            used_maps_in_series.add(map_name)
            if mode_name in {"Control", "Escort", "Hybrid"}:
                if len(cycle_modes_played) == 3:
                    cycle_modes_played = set()
                cycle_modes_played.add(mode_name)

    hero_pool_dated_scrim_dates = [
        parsed
        for parsed in (
            _parse_scrim_date(scrim.get("scrim_date", ""))
            or _infer_date_from_season(scrim.get("season", ""))
            for scrim in hero_pool_scrims
        )
        if parsed is not None
    ]
    newest_hero_pool_date = max(hero_pool_dated_scrim_dates) if hero_pool_dated_scrim_dates else None
    earliest_hero_pool_date = min(hero_pool_dated_scrim_dates) if hero_pool_dated_scrim_dates else None
    hero_pool_undated_offset = (
        (newest_hero_pool_date - earliest_hero_pool_date).days + 1
        if newest_hero_pool_date and earliest_hero_pool_date else 0
    )

    hero_weighted_apps = defaultdict(float)
    hero_weighted_wins = defaultdict(float)
    hero_raw_apps = defaultdict(int)
    hero_weighted_players = defaultdict(set)
    hero_player_weighted_apps = defaultdict(lambda: defaultdict(float))
    hero_player_weighted_wins = defaultdict(lambda: defaultdict(float))
    hero_player_raw_apps = defaultdict(lambda: defaultdict(int))
    hero_trend_points = defaultdict(list)
    hero_pool_weighted_total_maps = 0.0
    hero_pool_weighted_total_wins = 0.0
    hero_pool_map_time_index = 0

    for _scrim_idx, scrim in sorted(enumerate(hero_pool_scrims), key=scrim_sort_key):
        scrim_date = (
            _parse_scrim_date(scrim.get("scrim_date", ""))
            or _infer_date_from_season(scrim.get("season", ""))
        )
        recency_weight = 1.0
        if newest_hero_pool_date is not None and scrim_date is not None:
            age_days = max(0, (newest_hero_pool_date - scrim_date).days)
            recency_weight = math.exp(-recency_decay_lambda * age_days)

        ordered_maps = [map_entry for map_entry in scrim.get("maps", []) if isinstance(map_entry, dict)]
        for map_entry in ordered_maps:
            hero_pool_map_time_index += 1
            if scrim_date is not None and earliest_hero_pool_date is not None:
                time_x = float((scrim_date - earliest_hero_pool_date).days)
            else:
                time_x = float(hero_pool_undated_offset + hero_pool_map_time_index)
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            outcome = get_map_outcome_for_slot(map_entry, our_team_slot)
            decided_outcome = 1.0 if outcome == "Win" else 0.0 if outcome == "Loss" else None
            map_weight = map_type_weight(map_entry.get("map_type", "")) * recency_weight
            hero_pool_weighted_total_maps += map_weight
            if outcome == "Win":
                hero_pool_weighted_total_wins += map_weight

            map_seen_heroes: set[str] = set()
            map_seen_hero_players: set[tuple[str, str]] = set()
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
                    if hero_name not in map_seen_heroes:
                        hero_weighted_apps[hero_name] += map_weight
                        hero_raw_apps[hero_name] += 1
                        if outcome == "Win":
                            hero_weighted_wins[hero_name] += map_weight
                        map_seen_heroes.add(hero_name)
                    if player_name:
                        hero_weighted_players[hero_name].add(player_name)
                        hero_player_key = (hero_name, player_name)
                        if hero_player_key not in map_seen_hero_players:
                            hero_player_weighted_apps[hero_name][player_name] += map_weight
                            hero_player_raw_apps[hero_name][player_name] += 1
                            if outcome == "Win":
                                hero_player_weighted_wins[hero_name][player_name] += map_weight
                            map_seen_hero_players.add(hero_player_key)

            if decided_outcome is not None:
                for hero_name in map_seen_heroes:
                    hero_trend_points[hero_name].append((time_x, decided_outcome, map_weight))

    hero_trend_deltas = {
        hero_name: weighted_linear_delta_pct(points)
        for hero_name, points in hero_trend_points.items()
    }
    comp_trend_deltas = {
        comp_key: weighted_linear_delta_pct(points)
        for comp_key, points in comp_trend_points.items()
    }

    overall_wr = (hero_pool_weighted_total_wins / hero_pool_weighted_total_maps) if hero_pool_weighted_total_maps else 0.0
    hero_lookup: dict[str, dict] = {}
    for hero_name, appearances in hero_weighted_apps.items():
        # comfort = play rate: fraction of maps where this hero was played (0.0–1.0).
        # Using hero_pool_weighted_total_maps (not sum of all hero picks) so that a hero
        # played every map gets comfort=1.0 rather than ~1/6.
        comfort = (appearances / hero_pool_weighted_total_maps) if hero_pool_weighted_total_maps else 0.0
        raw_wr = (hero_weighted_wins[hero_name] / appearances) if appearances else 0.0
        confidence = min(1.0, appearances / 20.0) if appearances else 0.0
        adjusted_wr = (confidence * raw_wr) + ((1.0 - confidence) * overall_wr)
        base_profile_score = ((comfort * 0.6) + (adjusted_wr * 0.4)) * 100
        trend_delta = hero_trend_deltas.get(hero_name, 0.0)
        trend_confidence = min(1.0, appearances / 10.0) if appearances else 0.0
        trend_bonus = max(
            -MACHINE_HERO_TREND_CAP,
            min(MACHINE_HERO_TREND_CAP, trend_delta * MACHINE_HERO_TREND_BLEND * trend_confidence),
        )
        profile_score = round(max(0.0, min(100.0, base_profile_score + trend_bonus)), 1)
        player_rows = []
        for player_name, player_apps in sorted(
            hero_player_weighted_apps.get(hero_name, {}).items(),
            key=lambda item: (item[1], item[0].lower()),
            reverse=True,
        ):
            player_wr = (
                hero_player_weighted_wins[hero_name][player_name] / player_apps
                if player_apps else 0.0
            )
            player_rows.append(
                {
                    "player": player_name,
                    "maps": int(hero_player_raw_apps.get(hero_name, {}).get(player_name, 0) or 0),
                    "appearances": round(player_apps, 2),
                    "win_rate": round(player_wr * 100, 1),
                }
            )
        hero_lookup[hero_name] = {
            "hero": hero_name,
            "maps": int(hero_raw_apps.get(hero_name, 0) or 0),
            "appearances": round(appearances, 2),
            "comfort_rate": round(comfort * 100, 1),
            "raw_win_rate": round(raw_wr * 100, 1),
            "adjusted_win_rate": round(adjusted_wr * 100, 1),
            "confidence": round(confidence * 100, 1),
            "trend_delta_pp": round(trend_delta, 1),
            "trend_bonus": round(trend_bonus, 1),
            "base_profile_score": round(base_profile_score, 1),
            "player_count": len(hero_weighted_players.get(hero_name, set())),
            "primary_player": player_rows[0]["player"] if player_rows else "",
            "top_players": player_rows[:3],
            "profile_score": profile_score,
        }

    comfort_core_rows = sorted(
        [
            row
            for row in hero_lookup.values()
            if float(row.get("comfort_rate", 0) or 0) >= COMFORT_CORE_MIN_RATE
        ],
        key=lambda row: (row["comfort_rate"], row["appearances"], row["hero"].lower()),
        reverse=True,
    )[:12]

    hero_pool_rows = sorted(
        hero_lookup.values(),
        key=lambda row: (row["profile_score"], row["appearances"], row["hero"].lower()),
        reverse=True,
    )[:12]

    player_hero_apps: dict[str, float] = defaultdict(float)
    player_hero_wins: dict[str, float] = defaultdict(float)
    player_hero_breakdown: dict[str, list[dict]] = {}
    for hero_name, player_apps_map in hero_player_weighted_apps.items():
        for player_name, player_apps in player_apps_map.items():
            player_hero_apps[player_name] += float(player_apps or 0.0)
            player_hero_wins[player_name] += float(hero_player_weighted_wins.get(hero_name, {}).get(player_name, 0.0) or 0.0)

    for player_name, total_apps in player_hero_apps.items():
        hero_rows = []
        for hero_name, player_apps in hero_player_weighted_apps.items():
            apps = float(player_apps.get(player_name, 0.0) or 0.0)
            if apps <= 0:
                continue
            wins = float(hero_player_weighted_wins.get(hero_name, {}).get(player_name, 0.0) or 0.0)
            raw_maps = int(hero_player_raw_apps.get(hero_name, {}).get(player_name, 0) or 0)
            hero_rows.append(
                {
                    "hero": hero_name,
                    "maps": raw_maps,
                    "appearances": round(apps, 2),
                    "usage_rate": round((apps / total_apps) * 100, 1) if total_apps else 0.0,
                    "win_rate": round((wins / apps) * 100, 1) if apps else 0.0,
                }
            )
        hero_rows.sort(key=lambda row: (row["maps"], row["appearances"], row["usage_rate"], row["hero"].lower()), reverse=True)
        player_hero_breakdown[player_name] = hero_rows

    player_hero_rows = [
        {
            "player": player_name,
            "maps": int(sum(row.get("maps", 0) for row in player_hero_breakdown.get(player_name, []))),
            "appearances": round(total_apps, 2),
            "overall_win_rate": round((player_hero_wins.get(player_name, 0.0) / total_apps) * 100, 1) if total_apps else 0.0,
            "top_heroes": player_hero_breakdown.get(player_name, []),
        }
        for player_name, total_apps in sorted(
            player_hero_apps.items(),
            key=lambda item: (item[1], item[0].lower()),
            reverse=True,
        )
    ]

    total_complete_lines = sum(line_counts.values())
    ban_line_rows = []
    for line_key, count in sorted(line_counts.items(), key=lambda item: item[1], reverse=True)[:16]:
        heroes = list(line_key)
        hero_profiles = [hero_lookup.get(hero, {}) for hero in heroes if hero]
        avg_comfort = round(
            sum(float(profile.get("comfort_rate", 0) or 0) for profile in hero_profiles) / len(hero_profiles),
            1,
        ) if hero_profiles else 0.0
        avg_adjusted_wr = round(
            sum(float(profile.get("adjusted_win_rate", 0) or 0) for profile in hero_profiles) / len(hero_profiles),
            1,
        ) if hero_profiles else 0.0
        line_wr = round((line_wins[line_key] / count) * 100, 1) if count else 0.0
        line_strength = round((avg_comfort * 0.55) + (avg_adjusted_wr * 0.45), 1)
        # DRAFT_SLOT_ORDER = (ban1, protect1, ban2, ban3, protect2, ban4)
        # Indices 0,2,3,5 = bans;  indices 1,4 = protects
        slot_labels = list(DRAFT_SLOT_ORDER)
        bans = [heroes[i] for i in (0, 2, 3, 5) if i < len(heroes) and heroes[i]]
        protects = [heroes[i] for i in (1, 4) if i < len(heroes) and heroes[i]]
        slots = [
            {"label": slot_labels[i], "hero": heroes[i], "type": "protect" if slot_labels[i].startswith("protect") else "ban"}
            for i in range(len(heroes)) if i < len(slot_labels)
        ]
        ban_line_rows.append(
            {
                "heroes": heroes,
                "bans": bans,
                "protects": protects,
                "slots": slots,
                "count": count,
                "rate": round((count / total_complete_lines) * 100, 1) if total_complete_lines else 0.0,
                "avg_comfort": avg_comfort,
                "avg_adjusted_win_rate": avg_adjusted_wr,
                "line_win_rate": line_wr,
                "line_strength": line_strength,
            }
        )

    total_comps = sum(comp_counts.values())
    comp_rows = []
    # Bayesian win-probability model over comp outcomes.
    # This provides a lightweight learned estimate that is robust for low-sample comps.
    prior_strength = 12.0
    alpha0 = max(1e-6, overall_wr * prior_strength + 1.0)
    beta0 = max(1e-6, (1.0 - overall_wr) * prior_strength + 1.0)
    for comp_key, count in sorted(comp_counts.items(), key=lambda item: item[1], reverse=True)[:16]:
        heroes = list(comp_key)
        hero_profiles = [hero_lookup.get(hero, {}) for hero in heroes if hero]
        avg_comfort = round(
            sum(float(profile.get("comfort_rate", 0) or 0) for profile in hero_profiles) / len(hero_profiles),
            1,
        ) if hero_profiles else 0.0
        avg_adjusted_wr = round(
            sum(float(profile.get("adjusted_win_rate", 0) or 0) for profile in hero_profiles) / len(hero_profiles),
            1,
        ) if hero_profiles else 0.0
        comp_wr = round((comp_wins[comp_key] / count) * 100, 1) if count else 0.0
        comp_wins_weighted = float(comp_wins.get(comp_key, 0.0) or 0.0)
        posterior_win_prob = (
            (comp_wins_weighted + alpha0) / (count + alpha0 + beta0)
            if count > 0 else overall_wr
        )
        posterior_strength = count + alpha0 + beta0
        ml_confidence = min(100.0, (count / (count + prior_strength)) * 100.0) if count > 0 else 0.0
        uncertainty_penalty = (100.0 / math.sqrt(max(1.0, posterior_strength)))
        ml_comp_score = max(0.0, min(100.0, posterior_win_prob * 100.0 - uncertainty_penalty))
        comp_strength_base = (avg_comfort * 0.5) + (avg_adjusted_wr * 0.5)
        direct_comp_trend_delta = comp_trend_deltas.get(comp_key, 0.0)
        hero_trend_values = [hero_trend_deltas.get(hero_name, 0.0) for hero_name in heroes if hero_name]
        hero_avg_trend_delta = (sum(hero_trend_values) / len(hero_trend_values)) if hero_trend_values else 0.0
        if abs(direct_comp_trend_delta) > 0.05:
            comp_trend_delta = (direct_comp_trend_delta * 0.7) + (hero_avg_trend_delta * 0.3)
        else:
            # Exact 6-hero comps can be sparse; fall back to member-hero trend signal.
            comp_trend_delta = hero_avg_trend_delta
        comp_trend_confidence = min(1.0, count / 8.0) if count else 0.0
        comp_trend_bonus = max(
            -MACHINE_COMP_TREND_CAP,
            min(MACHINE_COMP_TREND_CAP, comp_trend_delta * MACHINE_COMP_TREND_BLEND * comp_trend_confidence),
        )
        comp_strength = round(max(0.0, min(100.0, comp_strength_base + comp_trend_bonus)), 1)
        comp_rows.append(
            {
                "heroes": heroes,
                "count": count,
                "rate": round((count / total_comps) * 100, 1) if total_comps else 0.0,
                "avg_comfort": avg_comfort,
                "avg_adjusted_win_rate": avg_adjusted_wr,
                "comp_win_rate": comp_wr,
                "ml_win_prob": round(posterior_win_prob * 100, 1),
                "ml_confidence": round(ml_confidence, 1),
                "ml_comp_score": round(ml_comp_score, 1),
                "comp_direct_trend_delta_pp": round(direct_comp_trend_delta, 1),
                "comp_hero_avg_trend_delta_pp": round(hero_avg_trend_delta, 1),
                "comp_trend_delta_pp": round(comp_trend_delta, 1),
                "comp_trend_bonus": round(comp_trend_bonus, 1),
                "comp_strength_base": round(comp_strength_base, 1),
                "comp_strength": comp_strength,
            }
        )

    comp_rows.sort(
        key=lambda row: (
            float(row.get("comp_strength", 0) or 0),
            float(row.get("rate", 0) or 0),
            float(row.get("count", 0) or 0),
        ),
        reverse=True,
    )

    comp_path_rows = []
    equivalent_path_rows = []
    top_comp_keys = [tuple(row["heroes"]) for row in comp_rows[:10] if row.get("heroes")]
    for comp_key in top_comp_keys:
        path_counts = comp_to_line_counts.get(comp_key, {})
        path_total = sum(path_counts.values())
        if not path_total:
            continue
        ordered_paths = sorted(path_counts.items(), key=lambda item: item[1], reverse=True)
        primary_line_key = ordered_paths[0][0]
        top_paths = []
        for line_key, count in ordered_paths[:4]:
            line_parts = split_line(line_key)
            top_paths.append(
                {
                    "heroes": list(line_key),
                    "bans": line_parts["bans"],
                    "protects": line_parts["protects"],
                    "count": round(count, 2),
                    "rate": round((count / path_total) * 100, 1) if path_total else 0.0,
                    "distance_from_primary": line_distance(line_key, primary_line_key),
                }
            )

        stable_slots = 0
        for slot_idx in range(len(primary_line_key)):
            slot_values = {path_key[slot_idx] for path_key, _ in ordered_paths[:3] if len(path_key) > slot_idx}
            if len(slot_values) == 1:
                stable_slots += 1

        path_distances = [
            line_distance(primary_line_key, path_key)
            for path_key, _ in ordered_paths[1:4]
        ]
        comp_path_rows.append(
            {
                "heroes": list(comp_key),
                "path_count": len(ordered_paths),
                "top_path_share": round((ordered_paths[0][1] / path_total) * 100, 1) if path_total else 0.0,
                "stable_slots": stable_slots,
                "deviation_budget": max(path_distances) if path_distances else 0,
                "avg_deviation": round(sum(path_distances) / len(path_distances), 1) if path_distances else 0.0,
                "top_draft_paths": top_paths,
            }
        )

        if len(ordered_paths) > 1:
            alt_line_key, alt_count = ordered_paths[1]
            primary_parts = split_line(primary_line_key)
            alt_parts = split_line(alt_line_key)
            equivalent_path_rows.append(
                {
                    "heroes": list(comp_key),
                    "primary_bans": primary_parts["bans"],
                    "primary_protects": primary_parts["protects"],
                    "alt_bans": alt_parts["bans"],
                    "alt_protects": alt_parts["protects"],
                    "primary_rate": round((ordered_paths[0][1] / path_total) * 100, 1) if path_total else 0.0,
                    "alt_rate": round((alt_count / path_total) * 100, 1) if path_total else 0.0,
                    "path_distance": line_distance(primary_line_key, alt_line_key),
                }
            )

    volatile_hero_rows = []
    for hero_name, appearances in hero_lookup.items():
        with_maps = hero_presence_maps.get(hero_name, 0.0)
        if not with_maps or weighted_total_maps <= with_maps:
            continue
        with_wr = (hero_presence_wins.get(hero_name, 0.0) / with_maps) if with_maps else 0.0
        without_maps = weighted_total_maps - with_maps
        without_wins = weighted_total_wins - hero_presence_wins.get(hero_name, 0.0)
        without_wr = (without_wins / without_maps) if without_maps else overall_wr
        delta = round((with_wr - without_wr) * 100, 1)
        volatile_hero_rows.append(
            {
                "hero": hero_name,
                "with_maps": round(with_maps, 2),
                "with_wr": round(with_wr * 100, 1),
                "without_wr": round(without_wr * 100, 1),
                "delta": delta,
                "volatility": abs(delta),
                "favored_side": "Team" if delta > 0 else "Enemy" if delta < 0 else "Neutral",
            }
        )
    volatile_hero_rows.sort(
        key=lambda row: (row["volatility"], row["with_maps"], row["hero"].lower()),
        reverse=True,
    )

    pivot_rows = []
    for comp_key in top_comp_keys[:6]:
        comp_set = set(comp_key)
        pivot_options = []
        for other_key, other_count in comp_counts.items():
            if other_key == comp_key:
                continue
            shared_count = len(comp_set & set(other_key))
            if shared_count < 3:
                continue
            diff_count = len(set(comp_key) ^ set(other_key))
            pivot_options.append(
                {
                    "heroes": list(other_key),
                    "shared_count": shared_count,
                    "diff_count": diff_count,
                    "rate": round((other_count / total_comps) * 100, 1) if total_comps else 0.0,
                }
            )
        pivot_options.sort(
            key=lambda row: (row["shared_count"], -row["diff_count"], row["rate"]),
            reverse=True,
        )
        if pivot_options:
            pivot_rows.append(
                {
                    "base_heroes": list(comp_key),
                    "pivots": pivot_options[:3],
                }
            )

    mode_hero_rows = []
    for mode_name, counts in mode_hero_counts.items():
        mode_total = mode_totals.get(mode_name, 0.0)
        if not mode_total:
            continue
        for hero_name, count in counts.items():
            overall_share = float(hero_lookup.get(hero_name, {}).get("comfort_rate", 0) or 0)
            mode_share = (count / mode_total) * 100 if mode_total else 0.0
            delta = round(mode_share - overall_share, 1)
            mode_hero_rows.append(
                {
                    "mode": mode_name,
                    "hero": hero_name,
                    "mode_rate": round(mode_share, 1),
                    "overall_rate": round(overall_share, 1),
                    "delta": delta,
                }
            )
    mode_hero_rows.sort(
        key=lambda row: (abs(row["delta"]), row["mode_rate"], row["hero"].lower()),
        reverse=True,
    )

    mode_comp_rows = []
    overall_comp_rate_lookup = {
        tuple(row["heroes"]): float(row.get("rate", 0) or 0)
        for row in comp_rows
    }
    for mode_name, counts in mode_comp_counts.items():
        mode_total = mode_comp_totals.get(mode_name, 0.0)
        if not mode_total:
            continue
        for comp_key, count in counts.items():
            mode_rate = (count / mode_total) * 100 if mode_total else 0.0
            overall_rate = overall_comp_rate_lookup.get(comp_key, 0.0)
            delta = round(mode_rate - overall_rate, 1)
            mode_comp_rows.append(
                {
                    "mode": mode_name,
                    "heroes": list(comp_key),
                    "mode_rate": round(mode_rate, 1),
                    "overall_rate": round(overall_rate, 1),
                    "delta": delta,
                }
            )
    mode_comp_rows.sort(
        key=lambda row: (abs(row["delta"]), row["mode_rate"]),
        reverse=True,
    )

    map_state_rows = []
    for label, counts in sorted(map_state_counts.items(), key=lambda item: map_state_totals.get(item[0], 0), reverse=True):
        total = map_state_totals.get(label, 0.0)
        options = [
            {
                "map_name": map_name,
                "mode": MAP_MODES.get(map_name, "Other"),
                "count": round(count, 2),
                "rate": round((count / total) * 100, 1) if total else 0.0,
            }
            for map_name, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:8]
        ]
        map_state_rows.append(
            {
                "label": label,
                "sample_weight": round(total, 2),
                "options": options,
            }
        )

    return {
        "status": "ready" if total_maps else "empty",
        "training_maps": total_maps,
        "weighted_maps": round(weighted_total_maps, 2),
        "uses_exponential_recency": True,
        "uses_weighted_linear_trend": True,
        "model_methods": {
            "recency_weighting": "exponential_decay",
            "trend_estimator": "weighted_linear_regression",
            "comp_outcome_model": "bayesian_beta_binomial",
        },
        "overall_win_rate": round(overall_wr * 100, 1),
        "comfort_core_rows": comfort_core_rows,
        "hero_pool_rows": hero_pool_rows,
        "player_hero_rows": player_hero_rows,
        "ban_line_rows": ban_line_rows,
        "comp_rows": comp_rows,
        "comp_path_rows": comp_path_rows[:8],
        "volatile_hero_rows": volatile_hero_rows[:12],
        "pivot_rows": pivot_rows[:6],
        "mode_hero_rows": mode_hero_rows[:10],
        "mode_comp_rows": mode_comp_rows[:8],
        "equivalent_path_rows": equivalent_path_rows[:8],
        "map_state_rows": map_state_rows,
        "map_type_weights": {"Standard": 1.0, "PTW": 1.75, "Test": 0.55},
        "recency_half_life_days": RECENCY_HALFLIFE_DAYS,
        "trend_model": {
            "min_points": MACHINE_TREND_MIN_POINTS,
            "hero_blend": MACHINE_HERO_TREND_BLEND,
            "comp_blend": MACHINE_COMP_TREND_BLEND,
            "hero_cap": MACHINE_HERO_TREND_CAP,
            "comp_cap": MACHINE_COMP_TREND_CAP,
        },
    }


def build_matchup_tree_model(
    team_a_name: str,
    team_a_scrims: list[dict],
    team_b_name: str,
    team_b_scrims: list[dict],
    team_a_hero_pool_scrims: list[dict] | None = None,
    team_b_hero_pool_scrims: list[dict] | None = None,
) -> dict:
    team_a_model = build_opponent_tree_model(team_a_scrims, hero_pool_scrims=team_a_hero_pool_scrims)
    team_b_model = build_opponent_tree_model(team_b_scrims, hero_pool_scrims=team_b_hero_pool_scrims)

    if (
        team_a_model.get("status") == "empty"
        and team_b_model.get("status") == "empty"
    ):
        return {
            "status": "empty",
            "teams": [
                {"name": team_a_name, "model": team_a_model},
                {"name": team_b_name, "model": team_b_model},
            ],
            "contested_heroes": [],
            "force_matchup_rows": [],
            "deviation_rows": [],
            "volatile_matchup_rows": [],
            "pivot_pressure_rows": [],
            "map_effect_rows": [],
            "equivalent_path_rows": [],
            "ban_clash_rows": [],
            "comp_clash_rows": [],
            "map_consensus_rows": [],
        }

    def hero_index(rows: list[dict]) -> dict[str, dict]:
        indexed: dict[str, dict] = {}
        for row in rows:
            hero_name = (row.get("hero", "") or "").strip()
            if hero_name:
                indexed[hero_name] = row
        return indexed

    def line_pair_score(line_a: dict, line_b: dict, overlap_count: int) -> tuple:
        return (
            overlap_count,
            round((float(line_a.get("rate", 0) or 0) + float(line_b.get("rate", 0) or 0)) / 2.0, 2),
            round((float(line_a.get("line_strength", 0) or 0) + float(line_b.get("line_strength", 0) or 0)) / 2.0, 2),
        )

    def comp_pair_score(comp_a: dict, comp_b: dict, overlap_count: int) -> tuple:
        return (
            overlap_count,
            round((float(comp_a.get("rate", 0) or 0) + float(comp_b.get("rate", 0) or 0)) / 2.0, 2),
            round((float(comp_a.get("comp_strength", 0) or 0) + float(comp_b.get("comp_strength", 0) or 0)) / 2.0, 2),
        )

    def conditioned_enemy_comp_rows(enemy_comp_paths: list[dict], our_path: dict) -> list[dict]:
        our_bans = set(our_path.get("bans", []))
        scored_rows = []
        for comp_row in enemy_comp_paths:
            heroes = [hero for hero in comp_row.get("heroes", []) if hero]
            blocked = len(our_bans & set(heroes))
            hero_count = len(heroes) or 1
            preserved_ratio = max(0.0, (hero_count - blocked) / hero_count)
            conditioned_rate = round(float(comp_row.get("top_path_share", 0) or 0) * preserved_ratio, 1)
            scored_rows.append(
                {
                    "heroes": heroes,
                    "blocked_heroes": sorted(our_bans & set(heroes)),
                    "blocked_count": blocked,
                    "preserved_ratio": round(preserved_ratio * 100, 1),
                    "conditioned_rate": conditioned_rate,
                    "top_path_share": float(comp_row.get("top_path_share", 0) or 0),
                    "path_count": int(comp_row.get("path_count", 0) or 0),
                }
            )
        scored_rows.sort(
            key=lambda row: (row["conditioned_rate"], -row["blocked_count"], row["top_path_share"]),
            reverse=True,
        )
        return scored_rows

    def build_player_pressure_rows(enemy_model: dict) -> list[dict]:
        player_rows = enemy_model.get("player_hero_rows", [])
        hero_pressure: dict[str, dict] = {}

        def hero_score(hero_row: dict) -> float:
            appearances = float(hero_row.get("appearances", 0) or 0)
            usage_rate = float(hero_row.get("usage_rate", 0) or 0)
            win_rate = float(hero_row.get("win_rate", 0) or 0)
            return (appearances * 2.5) + (usage_rate * 0.8) + ((win_rate - 50.0) * 0.18)

        for player_row in player_rows:
            player_name = (player_row.get("player", "") or "").strip()
            if not player_name:
                continue

            hero_rows = [
                row for row in (player_row.get("top_heroes", []) or [])
                if (row.get("hero") or "").strip() and float(row.get("appearances", 0) or 0) > 0
            ]
            if len(hero_rows) < 2:
                continue

            # Use most-played fallback first, then usage and win rate as tie-breakers.
            sorted_rows = sorted(
                hero_rows,
                key=lambda row: (
                    float(row.get("appearances", 0) or 0),
                    float(row.get("usage_rate", 0) or 0),
                    float(row.get("win_rate", 0) or 0),
                ),
                reverse=True,
            )
            primary = sorted_rows[0]
            fallback = sorted_rows[1]
            primary_hero = (primary.get("hero", "") or "").strip()
            fallback_hero = (fallback.get("hero", "") or "").strip()
            if not primary_hero or not fallback_hero:
                continue

            score_drop = max(0.0, hero_score(primary) - hero_score(fallback))
            if score_drop <= 0:
                continue

            bucket = hero_pressure.setdefault(
                primary_hero,
                {
                    "hero": primary_hero,
                    "affected_players": [],
                    "affected_count": 0,
                    "total_score_drop": 0.0,
                    "max_score_drop": 0.0,
                },
            )
            bucket["affected_players"].append(
                {
                    "player": player_name,
                    "fallback_hero": fallback_hero,
                    "score_drop": round(score_drop, 1),
                    "primary_appearances": int(float(primary.get("appearances", 0) or 0)),
                }
            )
            bucket["affected_count"] += 1
            bucket["total_score_drop"] += score_drop
            bucket["max_score_drop"] = max(float(bucket["max_score_drop"]), score_drop)

        rows = []
        for row in hero_pressure.values():
            affected_players = sorted(
                row["affected_players"],
                key=lambda p: (float(p.get("score_drop", 0) or 0), int(p.get("primary_appearances", 0) or 0)),
                reverse=True,
            )
            rows.append(
                {
                    "hero": row["hero"],
                    "affected_players": affected_players,
                    "affected_count": int(row["affected_count"]),
                    "total_score_drop": round(float(row["total_score_drop"]), 1),
                    "max_score_drop": round(float(row["max_score_drop"]), 1),
                }
            )

        rows.sort(
            key=lambda row: (
                int(row.get("affected_count", 0) or 0),
                float(row.get("total_score_drop", 0) or 0),
                float(row.get("max_score_drop", 0) or 0),
                (row.get("hero", "") or "").lower(),
            ),
            reverse=True,
        )
        return rows

    team_a_heroes = hero_index(team_a_model.get("comfort_core_rows", []))
    team_b_heroes = hero_index(team_b_model.get("comfort_core_rows", []))

    contested_heroes = []
    for hero_name in sorted(set(team_a_heroes) & set(team_b_heroes)):
        hero_a = team_a_heroes[hero_name]
        hero_b = team_b_heroes[hero_name]
        contested_heroes.append(
            {
                "hero": hero_name,
                "team_a_score": float(hero_a.get("profile_score", 0) or 0),
                "team_b_score": float(hero_b.get("profile_score", 0) or 0),
                "team_a_comfort": float(hero_a.get("comfort_rate", 0) or 0),
                "team_b_comfort": float(hero_b.get("comfort_rate", 0) or 0),
                "team_a_adj_wr": float(hero_a.get("adjusted_win_rate", 0) or 0),
                "team_b_adj_wr": float(hero_b.get("adjusted_win_rate", 0) or 0),
                "contested_score": round(
                    (
                        float(hero_a.get("profile_score", 0) or 0)
                        + float(hero_b.get("profile_score", 0) or 0)
                    ) / 2.0,
                    1,
                ),
            }
        )
    contested_heroes.sort(
        key=lambda row: (
            row["contested_score"],
            row["team_a_comfort"] + row["team_b_comfort"],
            row["hero"].lower(),
        ),
        reverse=True,
    )

    ban_clash_rows = []
    team_a_lines = team_a_model.get("ban_line_rows", [])[:6]
    team_b_lines = team_b_model.get("ban_line_rows", [])[:6]
    for line_a in team_a_lines:
        heroes_a = [hero for hero in line_a.get("heroes", []) if hero]
        hero_set_a = set(heroes_a)
        for line_b in team_b_lines:
            heroes_b = [hero for hero in line_b.get("heroes", []) if hero]
            overlap = sorted(hero_set_a & set(heroes_b))
            score_tuple = line_pair_score(line_a, line_b, len(overlap))
            ban_clash_rows.append(
                {
                    "team_a_heroes": heroes_a,
                    "team_b_heroes": heroes_b,
                    "shared_heroes": overlap,
                    "shared_count": len(overlap),
                    "combined_rate": score_tuple[1],
                    "combined_strength": score_tuple[2],
                }
            )
    ban_clash_rows.sort(
        key=lambda row: (
            row["shared_count"],
            row["combined_rate"],
            row["combined_strength"],
        ),
        reverse=True,
    )

    comp_clash_rows = []
    team_a_comps = team_a_model.get("comp_rows", [])[:6]
    team_b_comps = team_b_model.get("comp_rows", [])[:6]
    for comp_a in team_a_comps:
        heroes_a = [hero for hero in comp_a.get("heroes", []) if hero]
        hero_set_a = set(heroes_a)
        for comp_b in team_b_comps:
            heroes_b = [hero for hero in comp_b.get("heroes", []) if hero]
            overlap = sorted(hero_set_a & set(heroes_b))
            score_tuple = comp_pair_score(comp_a, comp_b, len(overlap))
            comp_clash_rows.append(
                {
                    "team_a_heroes": heroes_a,
                    "team_b_heroes": heroes_b,
                    "shared_heroes": overlap,
                    "shared_count": len(overlap),
                    "combined_rate": score_tuple[1],
                    "combined_strength": score_tuple[2],
                }
            )
    comp_clash_rows.sort(
        key=lambda row: (
            row["shared_count"],
            row["combined_rate"],
            row["combined_strength"],
        ),
        reverse=True,
    )

    state_lookup_a = {
        row.get("label", ""): row
        for row in team_a_model.get("map_state_rows", [])
        if row.get("label")
    }
    state_lookup_b = {
        row.get("label", ""): row
        for row in team_b_model.get("map_state_rows", [])
        if row.get("label")
    }
    map_consensus_rows = []
    for state_label in sorted(set(state_lookup_a) & set(state_lookup_b)):
        options_a = {
            option.get("map_name", ""): option
            for option in state_lookup_a[state_label].get("options", [])
            if option.get("map_name")
        }
        options_b = {
            option.get("map_name", ""): option
            for option in state_lookup_b[state_label].get("options", [])
            if option.get("map_name")
        }
        shared_maps = []
        for map_name in sorted(set(options_a) & set(options_b)):
            map_a = options_a[map_name]
            map_b = options_b[map_name]
            shared_maps.append(
                {
                    "map_name": map_name,
                    "image": get_map_image_url(map_name),
                    "mode": map_a.get("mode", "Other"),
                    "team_a_rate": float(map_a.get("rate", 0) or 0),
                    "team_b_rate": float(map_b.get("rate", 0) or 0),
                    "combined_rate": round(
                        (
                            float(map_a.get("rate", 0) or 0)
                            + float(map_b.get("rate", 0) or 0)
                        ) / 2.0,
                        1,
                    ),
                }
            )
        shared_maps.sort(
            key=lambda row: (row["combined_rate"], row["map_name"].lower()),
            reverse=True,
        )
        if shared_maps:
            map_consensus_rows.append(
                {
                    "label": state_label,
                    "options": shared_maps[:6],
                }
            )

    comp_ml_lookup_a = {
        tuple(row.get("heroes", [])): {
            "ml_win_prob": float(row.get("ml_win_prob", row.get("comp_win_rate", 0)) or 0),
            "ml_confidence": float(row.get("ml_confidence", 0) or 0),
            "ml_comp_score": float(row.get("ml_comp_score", row.get("comp_strength", 0)) or 0),
        }
        for row in team_a_model.get("comp_rows", [])
        if row.get("heroes")
    }

    force_matchup_rows = []
    team_a_comp_paths = team_a_model.get("comp_path_rows", [])[:6]
    team_b_comp_paths = team_b_model.get("comp_path_rows", [])[:6]
    for our_comp in team_a_comp_paths:
        our_paths = our_comp.get("top_draft_paths", [])
        if not our_paths:
            continue
        primary_path = our_paths[0]
        enemy_options = conditioned_enemy_comp_rows(team_b_comp_paths, primary_path)
        if not enemy_options:
            continue
        top_enemy = enemy_options[0]
        second_enemy = enemy_options[1] if len(enemy_options) > 1 else None
        force_matchup_rows.append(
            {
                "our_comp": our_comp.get("heroes", []),
                "enemy_comp": top_enemy["heroes"],
                "our_bans": primary_path.get("bans", []),
                "our_protects": primary_path.get("protects", []),
                "our_path_share": float(primary_path.get("rate", 0) or 0),
                "our_deviation_budget": our_comp.get("deviation_budget", 0),
                "enemy_blocked_count": top_enemy["blocked_count"],
                "enemy_blocked_heroes": top_enemy["blocked_heroes"],
                "enemy_choice_gap": round(
                    top_enemy["conditioned_rate"] - float(second_enemy["conditioned_rate"]) if second_enemy else top_enemy["conditioned_rate"],
                    1,
                ),
                "enemy_alt_comp": second_enemy["heroes"] if second_enemy else [],
                "enemy_conditioned_rate": top_enemy["conditioned_rate"],
                "enemy_preserved_ratio": top_enemy["preserved_ratio"],
                "micro_path_count": int(our_comp.get("path_count", 0) or 0),
                "ml_win_prob": comp_ml_lookup_a.get(tuple(our_comp.get("heroes", [])), {}).get("ml_win_prob", 0.0),
                "ml_confidence": comp_ml_lookup_a.get(tuple(our_comp.get("heroes", [])), {}).get("ml_confidence", 0.0),
                "ml_comp_score": comp_ml_lookup_a.get(tuple(our_comp.get("heroes", [])), {}).get("ml_comp_score", 0.0),
            }
        )
    force_matchup_rows.sort(
        key=lambda row: (
            ((float(row.get("ml_comp_score", 0) or 0) * 0.55)
             + (float(row.get("our_path_share", 0) or 0) * 0.25)
             + (float(row.get("enemy_choice_gap", 0) or 0) * 0.20)),
            row["our_path_share"],
            row["enemy_choice_gap"],
            -row["enemy_blocked_count"],
        ),
        reverse=True,
    )

    team_b_pressure_rows = build_player_pressure_rows(team_b_model)

    ml_outlook_rows = []
    for row in force_matchup_rows[:6]:
        our_comp = row.get("our_comp", [])
        our_bans = row.get("our_bans", [])
        enemy_comp = row.get("enemy_comp", [])
        pressure_bans = [
            {
                "hero": pressure_row.get("hero", ""),
                "affected_count": int(pressure_row.get("affected_count", 0) or 0),
                "total_score_drop": round(float(pressure_row.get("total_score_drop", 0) or 0), 1),
            }
            for pressure_row in team_b_pressure_rows
            if (pressure_row.get("hero", "") or "")
            and (pressure_row.get("hero", "") not in set(our_comp))
        ][:3]

        top_pressure_drop = float(pressure_bans[0].get("total_score_drop", 0) or 0) if pressure_bans else 0.0
        top_pressure_count = int(pressure_bans[0].get("affected_count", 0) or 0) if pressure_bans else 0
        enemy_preserved = float(row.get("enemy_preserved_ratio", 0) or 0)
        enemy_choice_gap = float(row.get("enemy_choice_gap", 0) or 0)
        ml_win_prob = float(row.get("ml_win_prob", 0) or 0)
        ml_confidence = float(row.get("ml_confidence", 0) or 0)
        ml_comp_score = float(row.get("ml_comp_score", 0) or 0)
        our_path_share = float(row.get("our_path_share", 0) or 0)
        blocked_count = int(row.get("enemy_blocked_count", 0) or 0)
        path_ban_set = set(our_bans)

        pressure_bans = [
            pressure for pressure in pressure_bans
            if (pressure.get("hero", "") or "") not in path_ban_set
        ]

        read_score = (
            (ml_win_prob * 0.28)
            + (ml_confidence * 0.16)
            + (ml_comp_score * 0.20)
            + ((100.0 - enemy_preserved) * 0.14)
            + (enemy_choice_gap * 0.10)
            + (our_path_share * 0.05)
            + (blocked_count * 2.2)
            + (top_pressure_drop * 0.10)
            + (top_pressure_count * 1.2)
        )
        if read_score >= 68:
            read_tier = "High Leverage"
        elif read_score >= 54:
            read_tier = "Strong"
        elif read_score >= 42:
            read_tier = "Playable"
        else:
            read_tier = "Volatile"

        ml_outlook_rows.append(
            {
                "our_comp": our_comp,
                "our_bans": our_bans,
                "our_protects": row.get("our_protects", []),
                "enemy_comp": enemy_comp,
                "enemy_alt_comp": row.get("enemy_alt_comp", []),
                "ml_win_prob": round(float(row.get("ml_win_prob", 0) or 0), 1),
                "ml_confidence": round(float(row.get("ml_confidence", 0) or 0), 1),
                "ml_comp_score": round(ml_comp_score, 1),
                "enemy_choice_gap": round(enemy_choice_gap, 1),
                "our_path_share": round(our_path_share, 1),
                "enemy_blocked_count": blocked_count,
                "enemy_blocked_heroes": row.get("enemy_blocked_heroes", []),
                "enemy_conditioned_rate": round(float(row.get("enemy_conditioned_rate", 0) or 0), 1),
                "enemy_preserved_ratio": round(enemy_preserved, 1),
                "pressure_bans": pressure_bans,
                "top_pressure_drop": round(top_pressure_drop, 1),
                "top_pressure_count": top_pressure_count,
                "read_score": round(read_score, 1),
                "read_tier": read_tier,
            }
        )

    ml_outlook_rows.sort(
        key=lambda out: (
            float(out.get("read_score", 0) or 0),
            float(out.get("ml_win_prob", 0) or 0),
            float(out.get("our_path_share", 0) or 0),
        ),
        reverse=True,
    )

    if ml_outlook_rows:
        score_values = sorted(
            [float(row.get("read_score", 0) or 0) for row in ml_outlook_rows],
            reverse=True,
        )

        def score_percentile(p: float) -> float:
            if not score_values:
                return 0.0
            index = int(round((len(score_values) - 1) * p))
            index = max(0, min(len(score_values) - 1, index))
            return float(score_values[index])

        high_cut = max(76.0, score_percentile(0.25))
        strong_cut = max(62.0, score_percentile(0.50))
        playable_cut = max(50.0, score_percentile(0.75))

        for outlook in ml_outlook_rows:
            score = float(outlook.get("read_score", 0) or 0)
            if score >= high_cut:
                tier = "High Leverage"
            elif score >= strong_cut:
                tier = "Strong"
            elif score >= playable_cut:
                tier = "Playable"
            else:
                tier = "Volatile"
            outlook["read_tier"] = tier

    deviation_rows = []
    for our_comp in team_a_comp_paths[:6]:
        draft_paths = our_comp.get("top_draft_paths", [])
        if len(draft_paths) < 2:
            continue
        primary_path = draft_paths[0]
        alt_path = draft_paths[1]
        primary_enemy = conditioned_enemy_comp_rows(team_b_comp_paths, primary_path)
        alt_enemy = conditioned_enemy_comp_rows(team_b_comp_paths, alt_path)
        primary_enemy_comp = primary_enemy[0]["heroes"] if primary_enemy else []
        alt_enemy_comp = alt_enemy[0]["heroes"] if alt_enemy else []
        deviation_rows.append(
            {
                "our_comp": our_comp.get("heroes", []),
                "primary_bans": primary_path.get("bans", []),
                "alt_bans": alt_path.get("bans", []),
                "path_distance": alt_path.get("distance_from_primary", 0),
                "stable_slots": our_comp.get("stable_slots", 0),
                "deviation_budget": our_comp.get("deviation_budget", 0),
                "enemy_primary_comp": primary_enemy_comp,
                "enemy_alt_comp": alt_enemy_comp,
                "enemy_response_changed": primary_enemy_comp != alt_enemy_comp,
                "enemy_response_shift": round(
                    (float(primary_enemy[0]["conditioned_rate"]) - float(alt_enemy[0]["conditioned_rate"]))
                    if primary_enemy and alt_enemy
                    else 0.0,
                    1,
                ),
            }
        )
    deviation_rows.sort(
        key=lambda row: (row["deviation_budget"], row["path_distance"], row["enemy_response_changed"]),
        reverse=True,
    )

    volatile_matchup_rows = []
    volatile_lookup_a = {row["hero"]: row for row in team_a_model.get("volatile_hero_rows", [])}
    volatile_lookup_b = {row["hero"]: row for row in team_b_model.get("volatile_hero_rows", [])}
    # Only consider heroes with meaningful appearances in at least one team.
    hero_pool_a = {row["hero"] for row in team_a_model.get("hero_pool_rows", [])}
    hero_pool_b = {row["hero"] for row in team_b_model.get("hero_pool_rows", [])}
    relevant_volatile_heroes = (set(volatile_lookup_a) | set(volatile_lookup_b)) & (hero_pool_a | hero_pool_b)
    for hero_name in sorted(relevant_volatile_heroes):
        row_a = volatile_lookup_a.get(hero_name, {})
        row_b = volatile_lookup_b.get(hero_name, {})
        delta_a = float(row_a.get("delta", 0) or 0)
        delta_b = float(row_b.get("delta", 0) or 0)
        combined = round(abs(delta_a) + abs(delta_b), 1)
        volatile_matchup_rows.append(
            {
                "hero": hero_name,
                "team_a_delta": delta_a,
                "team_a_with_wr": float(row_a.get("with_wr", 0) or 0),
                "team_a_without_wr": float(row_a.get("without_wr", 0) or 0),
                "team_a_with_maps": float(row_a.get("with_maps", 0) or 0),
                "team_b_delta": delta_b,
                "team_b_with_wr": float(row_b.get("with_wr", 0) or 0),
                "team_b_without_wr": float(row_b.get("without_wr", 0) or 0),
                "team_b_with_maps": float(row_b.get("with_maps", 0) or 0),
                "combined_volatility": combined,
                "favored_side": team_a_name if delta_a > delta_b else team_b_name if delta_b > delta_a else "Even",
            }
        )
    volatile_matchup_rows.sort(
        key=lambda row: (row["combined_volatility"], row["hero"].lower()),
        reverse=True,
    )

    pivot_pressure_rows = []
    pivot_rows_a = team_a_model.get("pivot_rows", [])[:4]
    pivot_rows_b = team_b_model.get("pivot_rows", [])[:4]
    for enemy_pivot in pivot_rows_b:
        enemy_base = enemy_pivot.get("base_heroes", [])
        top_enemy_pivot = (enemy_pivot.get("pivots") or [None])[0]
        if not top_enemy_pivot:
            continue
        best_counter = None
        best_counter_score = None
        for our_pivot in pivot_rows_a:
            for option in our_pivot.get("pivots", []):
                shared = len(set(option.get("heroes", [])) & set(top_enemy_pivot.get("heroes", [])))
                counter_score = (shared * -1, option.get("diff_count", 0), option.get("rate", 0))
                if best_counter_score is None or counter_score < best_counter_score:
                    best_counter_score = counter_score
                    best_counter = {
                        "base_heroes": our_pivot.get("base_heroes", []),
                        "heroes": option.get("heroes", []),
                        "shared": shared,
                        "diff_count": option.get("diff_count", 0),
                    }
        pivot_pressure_rows.append(
            {
                "enemy_base": enemy_base,
                "enemy_pivot": top_enemy_pivot.get("heroes", []),
                "enemy_diff_count": top_enemy_pivot.get("diff_count", 0),
                "our_counter_base": best_counter.get("base_heroes", []) if best_counter else [],
                "our_counter_pivot": best_counter.get("heroes", []) if best_counter else [],
                "our_counter_diff_count": best_counter.get("diff_count", 0) if best_counter else 0,
                "shared_after_pivot": best_counter.get("shared", 0) if best_counter else 0,
            }
        )

    map_effect_rows = []
    for row in team_a_model.get("mode_hero_rows", [])[:5]:
        map_effect_rows.append(
            {
                "scope": "Hero",
                "team": team_a_name,
                "mode": row.get("mode", ""),
                "label": row.get("hero", ""),
                "delta": row.get("delta", 0),
            }
        )
    for row in team_b_model.get("mode_hero_rows", [])[:5]:
        map_effect_rows.append(
            {
                "scope": "Hero",
                "team": team_b_name,
                "mode": row.get("mode", ""),
                "label": row.get("hero", ""),
                "delta": row.get("delta", 0),
            }
        )
    for row in team_a_model.get("mode_comp_rows", [])[:3]:
        map_effect_rows.append(
            {
                "scope": "Comp",
                "team": team_a_name,
                "mode": row.get("mode", ""),
                "label": ", ".join(row.get("heroes", [])[:3]),
                "delta": row.get("delta", 0),
            }
        )
    for row in team_b_model.get("mode_comp_rows", [])[:3]:
        map_effect_rows.append(
            {
                "scope": "Comp",
                "team": team_b_name,
                "mode": row.get("mode", ""),
                "label": ", ".join(row.get("heroes", [])[:3]),
                "delta": row.get("delta", 0),
            }
        )
    map_effect_rows.sort(
        key=lambda row: (abs(float(row.get("delta", 0) or 0)), row.get("mode", "")),
        reverse=True,
    )

    equivalent_path_rows = []
    # Build a fast lookup: for each ban-line hero list, what heroes are in each team's ban lines.
    team_b_ban_hero_sets = [
        set(hero for hero in ban_line.get("heroes", []) if hero)
        for ban_line in team_b_model.get("ban_line_rows", [])[:8]
    ]
    team_a_ban_hero_sets = [
        set(hero for hero in ban_line.get("heroes", []) if hero)
        for ban_line in team_a_model.get("ban_line_rows", [])[:8]
    ]
    for row in team_a_model.get("equivalent_path_rows", [])[:4]:
        primary_ban_heroes = set(row.get("primary_bans", []))
        # Primary ban heroes that Team B commonly bans — these are the ones that would force Team A onto the alt path.
        enemy_pressure = []
        for ban_set in team_b_ban_hero_sets:
            for hero in ban_set & primary_ban_heroes:
                if hero not in enemy_pressure:
                    enemy_pressure.append(hero)
        equivalent_path_rows.append(
            {
                "team": team_a_name,
                "enemy_ban_pressure": enemy_pressure[:4],
                **row,
            }
        )
    for row in team_b_model.get("equivalent_path_rows", [])[:4]:
        primary_ban_heroes = set(row.get("primary_bans", []))
        # Primary ban heroes that Team A commonly bans — these are the ones that would force Team B onto the alt path.
        enemy_pressure = []
        for ban_set in team_a_ban_hero_sets:
            for hero in ban_set & primary_ban_heroes:
                if hero not in enemy_pressure:
                    enemy_pressure.append(hero)
        equivalent_path_rows.append(
            {
                "team": team_b_name,
                "enemy_ban_pressure": enemy_pressure[:4],
                **row,
            }
        )

    return {
        "status": "ready",
        "teams": [
            {"name": team_a_name, "model": team_a_model},
            {"name": team_b_name, "model": team_b_model},
        ],
        "contested_heroes": contested_heroes[:10],
        "force_matchup_rows": force_matchup_rows[:6],
        "deviation_rows": deviation_rows[:6],
        "volatile_matchup_rows": volatile_matchup_rows[:8],
        "pivot_pressure_rows": pivot_pressure_rows[:6],
        "map_effect_rows": map_effect_rows[:10],
        "equivalent_path_rows": equivalent_path_rows[:8],
        "ban_clash_rows": ban_clash_rows[:8],
        "comp_clash_rows": comp_clash_rows[:8],
        "map_consensus_rows": map_consensus_rows[:4],
        "ml_outlook_rows": ml_outlook_rows,
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
                "image": get_map_image_url(map_name),
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
    map_log_rows = []

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

            map_log_rows.append(
                {
                    "scrim_id": scrim.get("id"),
                    "map_id": map_entry.get("id"),
                    "scrim_date": (scrim.get("scrim_date") or "").strip(),
                    "opponent_name": (scrim.get("enemy_team") or scrim.get("opponent") or "Unknown").strip() or "Unknown",
                    "map_name": map_name or "Unknown Map",
                    "result": result or "Not Set",
                    "instances": map_instances,
                }
            )

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
                "image": get_map_image_url(map_name),
            }
        )
    map_rows.sort(key=lambda r: (r["maps"], r["win_rate"]), reverse=True)
    map_log_rows.sort(
        key=lambda row: (
            row.get("scrim_date", ""),
            int(row.get("scrim_id") or 0),
            int(row.get("map_id") or 0),
        ),
        reverse=True,
    )

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
        "map_log_rows": map_log_rows,
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
    dashboard_sim_teams = [
        {
            "id": row["id"],
            "name": row["name"],
            "is_personal": bool(row["is_personal"]),
        }
        for row in all_team_rows
    ]
    dashboard_default_team_id = None
    for row in dashboard_sim_teams:
        if row["is_personal"]:
            dashboard_default_team_id = row["id"]
            break
    if dashboard_default_team_id is None and dashboard_sim_teams:
        dashboard_default_team_id = dashboard_sim_teams[0]["id"]
    dashboard_compare_team_id = None
    for row in dashboard_sim_teams:
        if row["id"] != dashboard_default_team_id:
            dashboard_compare_team_id = row["id"]
            break
    if dashboard_compare_team_id is None:
        dashboard_compare_team_id = dashboard_default_team_id

    dashboard_season_options = get_scrim_season_options(SCRIMS)
    dashboard_has_unseasoned_scrims = any(
        not normalize_season_value(scrim.get("season", ""))
        for scrim in SCRIMS
    )
    dashboard_default_season = get_current_season_from_recent_scrim(SCRIMS)

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
        dashboard_sim_teams=dashboard_sim_teams,
        dashboard_default_team_id=dashboard_default_team_id,
        dashboard_compare_team_id=dashboard_compare_team_id,
        dashboard_season_options=dashboard_season_options,
        dashboard_default_season=dashboard_default_season,
        dashboard_has_unseasoned_scrims=dashboard_has_unseasoned_scrims,
        map_type_options=MAP_TYPES,
        maps=MAPS,
        hero_roles=HERO_ROLES,
    )


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

    scrim_log = build_scrim_log_rows(
        team_scrims,
        team_slots=TEAM_SLOTS,
        canonical_draft_hero=_canonical_draft_hero,
        hero_match_key=_hero_match_key,
        hero_role=_hero_role,
        opposite_team_slot=opposite_team_slot,
        get_map_outcome_for_slot=get_map_outcome_for_slot,
        infer_result_from_score_text=infer_result_from_score_text,
        split_score_pair=split_score_pair,
    )
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
    archive_bytes = build_scrim_log_export_archive(
        team["name"],
        filtered_rows,
        normalize_match_team_slot=normalize_match_team_slot,
        opposite_team_slot=opposite_team_slot,
        simulator_slot_order=SIMULATOR_SLOT_ORDER,
    )
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

    map_overview = build_team_map_overview(
        team_scrims,
        map_modes=MAP_MODES,
        map_types=MAP_TYPES,
        normalize_map_type_value=normalize_map_type_value,
        get_map_outcome_for_slot=get_map_outcome_for_slot,
        get_map_image_url=get_map_image_url,
    )
    team_map_cards = map_overview["team_map_cards"]
    team_map_mode_rows = map_overview["team_map_mode_rows"]
    best_mode = map_overview["best_mode"]
    worst_mode = map_overview["worst_mode"]
    map_type_visual_rows = map_overview["map_type_visual_rows"]
    opponent_visual_rows = map_overview["opponent_visual_rows"]
    recent_map_visual_rows = map_overview["recent_map_visual_rows"]

    player_rows = db.execute(
        "SELECT * FROM players WHERE team_id = ? ORDER BY is_sub ASC, name COLLATE NOCASE",
        (team_id,),
    ).fetchall()

    players = []
    for row in player_rows:
        stats = compute_player_stats(row["name"], team_scrims)
        player_breakdown = build_player_hero_map_breakdown(
            row["name"],
            team_scrims,
            team_slots=TEAM_SLOTS,
            canonical_draft_hero=_canonical_draft_hero,
            get_map_outcome_for_slot=get_map_outcome_for_slot,
            map_modes=MAP_MODES,
            get_map_image_url=get_map_image_url,
        )
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

    team_hero_profile = build_team_hero_profile(
        team_scrims,
        players,
        team_slots=TEAM_SLOTS,
        canonical_draft_hero=_canonical_draft_hero,
        get_map_outcome_for_slot=get_map_outcome_for_slot,
    )
    hero_graph_rows = team_hero_profile.get("top_heroes", [])
    hero_usage_timeline = build_hero_usage_timeline(
        team_scrims,
        [row["hero"] for row in hero_graph_rows[:6]],
        team_slots=TEAM_SLOTS,
        canonical_map_hero_instances=_canonical_map_hero_instances,
    )

    team_ban_impact = build_team_ban_impact(team_scrims)

    atk_def_wr = build_atk_def_wr(team_scrims, attack_defense_maps=ATTACK_DEFENSE_MAPS)
    pivot_wr = build_pivot_wr(team_scrims, attack_defense_maps=ATTACK_DEFENSE_MAPS)
    scrim_log = build_scrim_log_rows(
        team_scrims,
        team_slots=TEAM_SLOTS,
        canonical_draft_hero=_canonical_draft_hero,
        hero_match_key=_hero_match_key,
        hero_role=_hero_role,
        opposite_team_slot=opposite_team_slot,
        get_map_outcome_for_slot=get_map_outcome_for_slot,
        infer_result_from_score_text=infer_result_from_score_text,
        split_score_pair=split_score_pair,
    )
    # Enrich team_map_cards with per-map attack/defense averages
    _atk_def_by_map = {row["map_name"]: row for row in atk_def_wr["per_map"]}
    for _card in team_map_cards:
        _stats = _atk_def_by_map.get(_card["map_name"])
        _card["attack_score_avg"] = _stats["atk_avg"] if _stats else None
        _card["defense_score_avg"] = _stats["def_avg"] if _stats else None

    matchup_context = build_team_detail_matchup_context(
        team_scrims,
        players,
        team_slots=TEAM_SLOTS,
        canonical_draft_hero=_canonical_draft_hero,
        hero_match_key=_hero_match_key,
        hero_role=_hero_role,
        opposite_team_slot=opposite_team_slot,
        get_map_outcome_for_slot=get_map_outcome_for_slot,
    )
    matchup_summary = matchup_context["matchup_summary"]
    matchup_rows = matchup_context["matchup_rows"]
    matrix_map_columns = matchup_context["matrix_map_columns"]
    matrix_rows = matchup_context["matrix_rows"]

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

    # Sync roster from DB (same pattern as tournament_detail) so players added to
    # the DB after the tournament was created are reflected here.
    _, db_players = _resolve_team_from_db(tournament_team.get("name", ""))
    if db_players:
        existing = set(tournament_team.get("players", []))
        changed = False
        for p in db_players:
            if p and p not in existing:
                tournament_team.setdefault("players", []).append(p)
                existing.add(p)
                changed = True
        if changed:
            save_app_state()

    team_scrims = build_tournament_team_scrims(tournament_record, tournament_team)
    team_analytics = build_scrim_analytics(team_scrims)
    hero_graph_rows = [
        {
            "hero": row["hero"],
            "maps": row["maps"],
            "appearances": row["maps"],
            "pick_rate": round((row["maps"] / team_analytics["summary"]["total_maps"]) * 100, 1)
            if team_analytics["summary"]["total_maps"]
            else 0,
            "usage_rate": round((row["maps"] / team_analytics["summary"]["total_maps"]) * 100, 1)
            if team_analytics["summary"]["total_maps"]
            else 0,
            "win_rate": row.get("win_rate", row.get("unmirrored_win_rate", 0)),
            "unmirrored_win_rate": row["unmirrored_win_rate"],
        }
        for row in team_analytics.get("hero_rows", [])
    ]
    hero_usage_timeline = build_hero_usage_timeline(
        team_scrims,
        [row["hero"] for row in hero_graph_rows[:6]],
        team_slots=TEAM_SLOTS,
        canonical_map_hero_instances=_canonical_map_hero_instances,
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
                "win_rate": round((wins / (wins + losses)) * 100, 1) if (wins + losses) else 0,
            }
        )

    match_rows.sort(key=lambda row: (row.get("scrim_date") or "", row.get("id") or 0), reverse=True)

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
                "image": get_map_image_url(map_name),
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


@app.route("/teams/<int:team_id>/opponent-tree")
def team_opponent_tree(team_id: int):
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    all_team_scrims = get_team_history_scrims(team)
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
    hero_pool_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
    hero_pool_scrims = filter_scrims_by_map_type(hero_pool_scrims, selected_map_type)
    return jsonify(build_opponent_tree_model(team_scrims, hero_pool_scrims=hero_pool_scrims))


@app.route("/teams/matchup-tree")
def team_matchup_tree():
    db = get_db()
    team_a_id = request.args.get("team_a", type=int)
    team_b_id = request.args.get("team_b", type=int)
    if not team_a_id or not team_b_id:
        return jsonify({"error": "Both team_a and team_b are required."}), 400

    team_rows = db.execute(
        "SELECT * FROM teams WHERE id IN (?, ?)",
        (team_a_id, team_b_id),
    ).fetchall()
    team_lookup = {row["id"]: row for row in team_rows}
    team_a = team_lookup.get(team_a_id)
    team_b = team_lookup.get(team_b_id)
    if team_a is None or team_b is None:
        abort(404)

    selected_map_type = get_selected_map_type(request.args.get("map_type", "all"))
    season_value = request.args.get("season", "")
    selected_map_name = (request.args.get("map", "") or "").strip()
    include_scrims = _bool_arg("include_scrims", True)
    include_tournaments = _bool_arg("include_tournaments", True)

    def filtered_scrims_for(team_row) -> tuple[list[dict], list[dict]]:
        all_team_scrims = get_team_history_for_sources(
            team_row,
            include_scrims=include_scrims,
            include_tournaments=include_tournaments,
        )
        season_options = get_scrim_season_options(all_team_scrims)
        default_season = get_current_season_from_recent_scrim(all_team_scrims)
        has_unseasoned_scrims = any(
            not normalize_season_value(scrim.get("season", ""))
            for scrim in all_team_scrims
        )
        # For the matchup model, default to "all" so the user's explicit
        # "All Seasons" selection is always respected.  Specific season
        # selections are still honoured normally.
        effective_default = "all" if not season_value or season_value.lower() == "all" else default_season
        selected_season = get_selected_season(
            season_value,
            season_options,
            allow_unspecified=has_unseasoned_scrims,
            default_season=effective_default,
            strict=True,
        )
        team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
        team_scrims = filter_scrims_by_map_type(team_scrims, selected_map_type)
        hero_pool_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
        hero_pool_scrims = filter_scrims_by_map_type(hero_pool_scrims, selected_map_type)
        if selected_map_name and selected_map_name.lower() != "all":
            filtered = []
            for scrim in team_scrims:
                scrim_copy = dict(scrim)
                scrim_copy["maps"] = [
                    m for m in scrim.get("maps", [])
                    if ((m.get("map") or m.get("map_name") or "").strip().lower() == selected_map_name.lower())
                ]
                if scrim_copy["maps"]:
                    filtered.append(scrim_copy)
            return filtered, hero_pool_scrims
        return team_scrims, hero_pool_scrims

    team_a_scrims, team_a_hero_pool_scrims = filtered_scrims_for(team_a)
    team_b_scrims, team_b_hero_pool_scrims = filtered_scrims_for(team_b)
    return jsonify(
        build_matchup_tree_model(
            team_a["name"],
            team_a_scrims,
            team_b["name"],
            team_b_scrims,
            team_a_hero_pool_scrims=team_a_hero_pool_scrims,
            team_b_hero_pool_scrims=team_b_hero_pool_scrims,
        )
    )


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
    breakdown = build_player_hero_map_breakdown(
        player["name"],
        team_scrims,
        team_slots=TEAM_SLOTS,
        canonical_draft_hero=_canonical_draft_hero,
        get_map_outcome_for_slot=get_map_outcome_for_slot,
        map_modes=MAP_MODES,
        get_map_image_url=get_map_image_url,
    )
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
        breakdown = build_player_hero_map_breakdown(
            player_row["name"],
            team_scrims,
            team_slots=TEAM_SLOTS,
            canonical_draft_hero=_canonical_draft_hero,
            get_map_outcome_for_slot=get_map_outcome_for_slot,
            map_modes=MAP_MODES,
            get_map_image_url=get_map_image_url,
        )
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

    filtered.sort(key=lambda s: s.get("scrim_date") or "", reverse=True)

    return render_template(
        "scrims.html",
        scrims=filtered,
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

    # 3-char prefix abbreviation: "Sentinels" → "sen", "Liquid" → "liq"
    if len(compact) >= 5:
        keys.add(compact[:3])

    # Initials from meaningful (non-generic) tokens only.
    # Use filtered_tokens so "Spacestation Gaming" → ["spacestation"] → only 1 token → no initials.
    # This prevents "Swamp Gaming" and "Spacestation Gaming" both generating "sg".
    if len(filtered_tokens) >= 2:
        meaningful_initials = "".join(t[0] for t in filtered_tokens if t[:1].isalpha())
        if len(meaningful_initials) >= 2:
            keys.add(meaningful_initials)
            keys.add("t" + meaningful_initials)

    # Extract explicit abbreviation from parenthetical hint: "Spacestation Gaming (SSG)" → "ssg"
    paren_match = re.search(r"\(([^)]+)\)", normalized)
    if paren_match:
        paren_abbrev = _compact_text(paren_match.group(1))
        if paren_abbrev:
            keys.add(paren_abbrev)

    return {key for key in keys if key}


def _team_names_match(left: str | None, right: str | None) -> bool:
    left_keys = _team_name_match_keys(left)
    right_keys = _team_name_match_keys(right)
    return bool(left_keys and right_keys and left_keys.intersection(right_keys))


def normalize_map_type_value(raw_value: str | None) -> str:
    normalized = _compact_text(raw_value or "")
    return MAP_TYPE_ALIASES.get(normalized, DEFAULT_MAP_TYPE)


def update_map_type_from_form(map_entry: dict) -> None:
    if "map_type" not in request.form:
        return
    map_entry["map_type"] = normalize_map_type_value(request.form.get("map_type", ""))


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

    if compact == "suppool":
        return "SupportPool"
    if compact.startswith("deadpool"):
        if any(token in compact for token in ("tank", "vanguard")):
            return "Tankpool"
        if any(token in compact for token in ("support", "strategist", "supp", "strat")):
            return "SupportPool"
        if any(token in compact for token in ("dps", "duelist", "duel")):
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
        "map_image_url": get_map_image_url,
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
    filtered_matches.sort(key=lambda m: m.get("scrim_date") or "", reverse=True)

    all_tournaments_sorted = sorted(TOURNAMENT_MATCHES, key=lambda m: m.get("scrim_date") or "", reverse=True)

    return render_template(
        "tournaments.html",
        tournaments=filtered_matches,
        all_tournaments=all_tournaments_sorted,
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
    if team_id is not None and not team_name:
        flash("Selected affiliated team could not be found.", "error")
        return redirect(f"{url_for('tournaments')}#create-tournament")

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

    normalize_tournament_record(tournament_match)
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


@app.route("/debug/scrims/<int:scrim_id>/move-to-tournament", methods=["POST"])
def debug_move_scrim_to_tournament(scrim_id: int):
    scrim = get_scrim_or_404(scrim_id)
    tournament_id = parse_team_id(request.form.get("tournament_id", ""))
    if tournament_id is None:
        flash("Choose an existing tournament to move this scrim into.", "error")
        return redirect(url_for("scrim_detail", scrim_id=scrim_id))

    tournament_record = get_tournament_or_404(tournament_id)
    participant_one, participant_two = get_scrim_participants(scrim)
    participant_one_label, participant_two_label = get_scrim_participant_labels(scrim)

    def _team_players(team_id: int | None) -> list[str]:
        if team_id is None:
            return []
        rows = get_db().execute(
            "SELECT name FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
            (team_id,),
        ).fetchall()
        return [row["name"] for row in rows if (row["name"] or "").strip()]

    def _find_or_add_tournament_team(team_id: int | None, team_name: str, fallback_players: list[str]) -> dict:
        normalized_name = (team_name or "").strip()
        for tournament_team in tournament_record.get("tournament_teams", []):
            if team_id is not None and tournament_team.get("source_team_id") == team_id:
                return tournament_team
            if normalized_name and str(tournament_team.get("name", "")).strip().lower() == normalized_name.lower():
                if team_id is not None and not tournament_team.get("source_team_id"):
                    tournament_team["source_team_id"] = team_id
                return tournament_team

        new_team = {
            "id": next_tournament_team_id(tournament_record),
            "name": normalized_name or "Unknown Team",
            "players": _team_players(team_id) or fallback_players,
        }
        if team_id is not None:
            new_team["source_team_id"] = team_id
        tournament_record.setdefault("tournament_teams", []).append(new_team)
        return new_team

    team1 = _find_or_add_tournament_team(
        participant_one.get("id"),
        participant_one_label or "Team 1",
        scrim.get("team1_players", []),
    )
    team2 = _find_or_add_tournament_team(
        participant_two.get("id"),
        participant_two_label or "Team 2",
        scrim.get("team2_players", []),
    )

    moved_maps = copy.deepcopy(scrim.get("maps", []))
    for map_entry in moved_maps:
        original_team1_id = map_entry.get("team1_id")
        original_team2_id = map_entry.get("team2_id")
        map_team1 = team1
        map_team2 = team2
        if original_team1_id == participant_two.get("id") or original_team2_id == participant_one.get("id"):
            map_team1 = team2
            map_team2 = team1
        map_entry["team1_tournament_team_id"] = map_team1["id"]
        map_entry["team2_tournament_team_id"] = map_team2["id"]
        map_entry["team1_name"] = map_team1["name"]
        map_entry["team2_name"] = map_team2["name"]
        map_entry["our_team_slot"] = "team1" if map_team1["id"] == team1["id"] else "team2"

    tournament_match = {
        "id": next_tournament_match_id(tournament_record),
        "scrim_date": scrim.get("scrim_date", ""),
        "notes": scrim.get("notes", ""),
        "team1_tournament_team_id": team1["id"],
        "team2_tournament_team_id": team2["id"],
        "team1_name": team1["name"],
        "team2_name": team2["name"],
        "maps": moved_maps,
    }
    tournament_record.setdefault("matches", []).append(tournament_match)
    normalize_tournament_record(tournament_record)
    SCRIMS.remove(scrim)
    save_app_state(allow_scrim_removal=True)
    flash("Moved scrim into the selected tournament.", "success")
    return redirect(url_for("tournament_match_detail", tournament_id=tournament_record["id"], match_id=tournament_match["id"]))


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
        tournaments=TOURNAMENT_MATCHES,
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

    # Sync each tournament team's roster from the DB on every page load
    roster_changed = False
    for t_team in tournament_record.get("tournament_teams", []):
        _, db_players = _resolve_team_from_db(t_team.get("name", ""))
        if db_players:
            existing = set(t_team.get("players", []))
            for p in db_players:
                if p and p not in existing:
                    t_team.setdefault("players", []).append(p)
                    existing.add(p)
                    roster_changed = True
    if roster_changed:
        save_app_state()

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
        map_images=MAP_IMAGES,
        completed_maps=completed_maps,
        today=date.today().isoformat(),
        hero_roles=HERO_ROLES,
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


@app.route("/tournaments/<int:tournament_id>/matches/import-file", methods=["POST"])
def import_tournament_match_file(tournament_id: int):
    global NEXT_MAP_ID

    tournament_record = get_tournament_or_404(tournament_id)
    uploaded_file = request.files.get("match_file")
    if uploaded_file is None or not uploaded_file.filename:
        flash("Choose a tournament match text file to import.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    try:
        file_bytes = uploaded_file.read()
        try:
            file_text = file_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            file_text = file_bytes.decode("latin-1")
        parsed_match = parse_tournament_match_text_import(file_text)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))
    except Exception:
        flash("Could not import that match file. Check the text format and try again.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    team1 = find_or_add_tournament_team_from_import(
        tournament_record,
        parsed_match["team1_name"],
        parsed_match.get("players_by_team", {}).get(parsed_match["team1_name"], []),
    )
    team2 = find_or_add_tournament_team_from_import(
        tournament_record,
        parsed_match["team2_name"],
        parsed_match.get("players_by_team", {}).get(parsed_match["team2_name"], []),
    )

    maps = []
    for parsed_map in parsed_match.get("maps", []):
        map_entry = copy.deepcopy(parsed_map)
        map_team1 = find_or_add_tournament_team_from_import(
            tournament_record,
            map_entry.get("team1_name", ""),
            parsed_match.get("players_by_team", {}).get(map_entry.get("team1_name", ""), []),
        )
        map_team2 = find_or_add_tournament_team_from_import(
            tournament_record,
            map_entry.get("team2_name", ""),
            parsed_match.get("players_by_team", {}).get(map_entry.get("team2_name", ""), []),
        )
        map_entry["id"] = NEXT_MAP_ID
        NEXT_MAP_ID += 1
        map_entry["team1_tournament_team_id"] = map_team1["id"]
        map_entry["team2_tournament_team_id"] = map_team2["id"]
        map_entry["team1_name"] = map_team1["name"]
        map_entry["team2_name"] = map_team2["name"]
        map_entry["our_team_slot"] = "team1" if map_team1["id"] == team1["id"] else "team2"
        map_entry["result"] = infer_result_from_score_text(map_entry.get("score", ""), slot=map_entry["our_team_slot"])
        map_entry["picked_by_tournament_team_id"] = None
        map_entry["picked_by_name"] = ""
        maps.append(map_entry)

    if not maps:
        flash("The uploaded file did not contain any map data.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    for index, map_entry in enumerate(maps):
        if index == 0:
            continue
        picker_id = get_tournament_map_loser_team_id(maps[index - 1])
        picker = find_tournament_team_by_id(tournament_record.get("tournament_teams", []), picker_id)
        if picker is not None:
            map_entry["picked_by_tournament_team_id"] = picker["id"]
            map_entry["picked_by_name"] = picker.get("name", "")

    match_date = request.form.get("scrim_date", "").strip() or tournament_record.get("scrim_date", "")
    notes = parsed_match.get("notes", "")
    if parsed_match.get("match_score"):
        notes = f"{notes}\nMatch score: {parsed_match['match_score']}".strip()
    tournament_match = {
        "id": next_tournament_match_id(tournament_record),
        "scrim_date": match_date,
        "notes": notes,
        "team1_tournament_team_id": team1["id"],
        "team2_tournament_team_id": team2["id"],
        "team1_name": team1["name"],
        "team2_name": team2["name"],
        "maps": maps,
    }
    normalize_tournament_match_record(tournament_match, tournament_record.get("tournament_teams", []))
    tournament_record.setdefault("matches", []).append(tournament_match)
    normalize_tournament_record(tournament_record)
    save_app_state()
    flash(f"Imported {team1['name']} vs {team2['name']} with {len(maps)} maps.", "success")
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
        add_comp_section_endpoint="add_tournament_match_comp_section",
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


def _collect_map_draft_intel(source_scrims: list[dict], map_name: str) -> dict:
    ban_slots = ("ban1", "ban2", "ban3", "ban4")
    protect_slots = ("protect1", "protect2")

    our_comp_counts = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    enemy_comp_counts = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})

    our_ban_counts = defaultdict(int)
    enemy_ban_counts = defaultdict(int)
    our_protect_counts = defaultdict(int)
    enemy_protect_counts = defaultdict(int)

    our_ban_slot_counts = defaultdict(lambda: defaultdict(int))
    enemy_ban_slot_counts = defaultdict(lambda: defaultdict(int))
    our_protect_slot_counts = defaultdict(lambda: defaultdict(int))
    enemy_protect_slot_counts = defaultdict(lambda: defaultdict(int))

    map_samples = 0
    our_total_bans = 0
    enemy_total_bans = 0
    our_total_protects = 0
    enemy_total_protects = 0

    def _largest_lineup_from_map(map_entry: dict, team_slot: str) -> list[str]:
        largest_lineup: list[str] = []
        for section in map_entry.get("comp", []):
            if not isinstance(section, dict):
                continue
            lineup = section.get(team_slot, [])
            if not isinstance(lineup, list):
                continue
            hero_rows: list[str] = []
            for slot in lineup:
                if not isinstance(slot, dict):
                    continue
                hero_name = _canonical_draft_hero(slot.get("hero", ""))
                if hero_name:
                    hero_rows.append(hero_name)
            if len(hero_rows) > len(largest_lineup):
                largest_lineup = hero_rows
        return largest_lineup

    for source_scrim in source_scrims:
        for map_entry in source_scrim.get("maps", []):
            if (map_entry.get("map_name") or "").strip() != map_name:
                continue

            map_samples += 1
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            enemy_team_slot = opposite_team_slot(our_team_slot)

            result = get_map_outcome_for_slot(map_entry, our_team_slot)

            our_lineup = _largest_lineup_from_map(map_entry, our_team_slot)
            enemy_lineup = _largest_lineup_from_map(map_entry, enemy_team_slot)
            if our_lineup:
                comp_key = tuple(sorted(our_lineup))
                our_comp_counts[comp_key]["count"] += 1
                if result == "Win":
                    our_comp_counts[comp_key]["wins"] += 1
                elif result == "Loss":
                    our_comp_counts[comp_key]["losses"] += 1
            if enemy_lineup:
                enemy_comp_key = tuple(sorted(enemy_lineup))
                enemy_comp_counts[enemy_comp_key]["count"] += 1
                if result == "Loss":
                    enemy_comp_counts[enemy_comp_key]["wins"] += 1
                elif result == "Win":
                    enemy_comp_counts[enemy_comp_key]["losses"] += 1

            draft = map_entry.get("draft", {})
            our_draft = draft.get(our_team_slot, {}) if isinstance(draft, dict) else {}
            enemy_draft = draft.get(enemy_team_slot, {}) if isinstance(draft, dict) else {}
            if not isinstance(our_draft, dict):
                our_draft = {}
            if not isinstance(enemy_draft, dict):
                enemy_draft = {}

            for slot_key in ban_slots:
                hero_name = _canonical_draft_hero(our_draft.get(slot_key, ""))
                if hero_name:
                    our_ban_counts[hero_name] += 1
                    our_ban_slot_counts[hero_name][slot_key] += 1
                    our_total_bans += 1
                enemy_hero_name = _canonical_draft_hero(enemy_draft.get(slot_key, ""))
                if enemy_hero_name:
                    enemy_ban_counts[enemy_hero_name] += 1
                    enemy_ban_slot_counts[enemy_hero_name][slot_key] += 1
                    enemy_total_bans += 1

            for slot_key in protect_slots:
                hero_name = _canonical_draft_hero(our_draft.get(slot_key, ""))
                if hero_name:
                    our_protect_counts[hero_name] += 1
                    our_protect_slot_counts[hero_name][slot_key] += 1
                    our_total_protects += 1
                enemy_hero_name = _canonical_draft_hero(enemy_draft.get(slot_key, ""))
                if enemy_hero_name:
                    enemy_protect_counts[enemy_hero_name] += 1
                    enemy_protect_slot_counts[enemy_hero_name][slot_key] += 1
                    enemy_total_protects += 1

    def _top_slot(slot_counts: dict[str, int]) -> tuple[str, int]:
        if not slot_counts:
            return "", 0
        slot_key, slot_count = max(slot_counts.items(), key=lambda item: (item[1], item[0]))
        return _draft_slot_label(slot_key), slot_count

    our_comp_rows = [
        {
            "heroes": list(comp_key),
            "count": stats["count"],
            "play_rate": round((stats["count"] / map_samples) * 100, 1) if map_samples else 0,
            "win_rate": round((stats["wins"] / stats["count"]) * 100, 1) if stats["count"] else 0,
        }
        for comp_key, stats in sorted(
            our_comp_counts.items(),
            key=lambda item: (item[1]["count"], item[1]["wins"], item[0]),
            reverse=True,
        )[:10]
    ]
    enemy_comp_rows = [
        {
            "heroes": list(comp_key),
            "count": stats["count"],
            "play_rate": round((stats["count"] / map_samples) * 100, 1) if map_samples else 0,
            "win_rate": round((stats["wins"] / stats["count"]) * 100, 1) if stats["count"] else 0,
        }
        for comp_key, stats in sorted(
            enemy_comp_counts.items(),
            key=lambda item: (item[1]["count"], item[1]["wins"], item[0]),
            reverse=True,
        )[:10]
    ]

    our_ban_rows = []
    for hero_name, count in sorted(our_ban_counts.items(), key=lambda item: (item[1], item[0]), reverse=True)[:12]:
        slot_label, slot_count = _top_slot(our_ban_slot_counts[hero_name])
        our_ban_rows.append(
            {
                "hero": hero_name,
                "count": count,
                "share": round((count / our_total_bans) * 100, 1) if our_total_bans else 0,
                "top_slot": slot_label,
                "top_slot_rate": round((slot_count / count) * 100, 1) if count else 0,
            }
        )

    enemy_ban_rows = []
    for hero_name, count in sorted(enemy_ban_counts.items(), key=lambda item: (item[1], item[0]), reverse=True)[:12]:
        slot_label, slot_count = _top_slot(enemy_ban_slot_counts[hero_name])
        enemy_ban_rows.append(
            {
                "hero": hero_name,
                "count": count,
                "share": round((count / enemy_total_bans) * 100, 1) if enemy_total_bans else 0,
                "top_slot": slot_label,
                "top_slot_rate": round((slot_count / count) * 100, 1) if count else 0,
            }
        )

    our_protect_rows = []
    for hero_name, count in sorted(our_protect_counts.items(), key=lambda item: (item[1], item[0]), reverse=True)[:10]:
        slot_label, slot_count = _top_slot(our_protect_slot_counts[hero_name])
        our_protect_rows.append(
            {
                "hero": hero_name,
                "count": count,
                "share": round((count / our_total_protects) * 100, 1) if our_total_protects else 0,
                "top_slot": slot_label,
                "top_slot_rate": round((slot_count / count) * 100, 1) if count else 0,
            }
        )

    enemy_protect_rows = []
    for hero_name, count in sorted(enemy_protect_counts.items(), key=lambda item: (item[1], item[0]), reverse=True)[:10]:
        slot_label, slot_count = _top_slot(enemy_protect_slot_counts[hero_name])
        enemy_protect_rows.append(
            {
                "hero": hero_name,
                "count": count,
                "share": round((count / enemy_total_protects) * 100, 1) if enemy_total_protects else 0,
                "top_slot": slot_label,
                "top_slot_rate": round((slot_count / count) * 100, 1) if count else 0,
            }
        )

    return {
        "our_comp_rows": our_comp_rows,
        "enemy_comp_rows": enemy_comp_rows,
        "our_ban_rows": our_ban_rows,
        "enemy_ban_rows": enemy_ban_rows,
        "our_protect_rows": our_protect_rows,
        "enemy_protect_rows": enemy_protect_rows,
        "map_samples": map_samples,
    }


@app.route("/scrims/<int:scrim_id>/timelines/<path:map_name>")
def scrim_map_timeline(scrim_id: int, map_name: str):
    scrim = get_scrim_or_404(scrim_id)
    participant_one_label, participant_two_label = get_scrim_participant_labels(scrim)
    participant_one, participant_two = get_scrim_participants(scrim)
    db = get_db()

    def _canonical_team_label(participant: dict, fallback: str) -> str:
        team_id_value = participant.get("id")
        if not team_id_value:
            return fallback
        row = db.execute("SELECT name FROM teams WHERE id = ?", (team_id_value,)).fetchone()
        if row is None:
            return fallback
        canonical_name = str(row["name"] or "").strip()
        return canonical_name or fallback

    participant_one_label = _canonical_team_label(participant_one, participant_one_label)
    participant_two_label = _canonical_team_label(participant_two, participant_two_label)

    team_id = participant_one.get("id") or scrim.get("team_id")
    team_name = (participant_one.get("name") or scrim.get("team_name") or scrim.get("team1_name") or "").strip()
    enemy_team_id = participant_two.get("id")
    enemy_team_name = (participant_two.get("name") or scrim.get("team2_name") or scrim.get("enemy_team") or scrim.get("opponent") or "").strip()
    map_timeline_row = None
    map_overview = {
        "maps": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0,
    }
    top_hero_rows: list[dict] = []
    enemy_top_hero_rows: list[dict] = []
    map_draft_intel = {
        "our_comp_rows": [],
        "enemy_comp_rows": [],
        "our_ban_rows": [],
        "enemy_ban_rows": [],
        "our_protect_rows": [],
        "enemy_protect_rows": [],
        "map_samples": 0,
    }
    if team_id and team_name:
        team_row = db.execute("SELECT id, name FROM teams WHERE id = ?", (team_id,)).fetchone()
        if team_row is not None:
            source_scrims = get_team_history_scrims(team_row)
        else:
            source_scrims = get_scrims_for_team(team_id, team_name)

        # Keep this page matchup-specific (Team A vs Team B), not broad all-opponents history.
        if enemy_team_id is not None or enemy_team_name:
            source_scrims = [
                source_scrim
                for source_scrim in source_scrims
                if scrim_involves_team(source_scrim, enemy_team_id, enemy_team_name)
            ]

        map_draft_intel = _collect_map_draft_intel(source_scrims, map_name)
        draft_timeline = build_draft_phase_timeline(source_scrims)
        map_timeline_row = next(
            (row for row in draft_timeline.get("maps", []) if row.get("map_name") == map_name),
            None,
        )

        our_hero_counts = defaultdict(int)
        our_hero_win_counts = defaultdict(int)
        enemy_hero_counts = defaultdict(int)
        enemy_hero_win_counts = defaultdict(int)
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
                is_enemy_win = result == "Loss"
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
                    if is_enemy_win:
                        enemy_hero_win_counts[hero_name] += 1

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
                "win_rate": round((enemy_hero_win_counts.get(hero_name, 0) / hero_maps) * 100, 1) if hero_maps else 0,
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
        our_comp_rows=map_draft_intel["our_comp_rows"],
        enemy_comp_rows=map_draft_intel["enemy_comp_rows"],
        our_ban_rows=map_draft_intel["our_ban_rows"],
        enemy_ban_rows=map_draft_intel["enemy_ban_rows"],
        our_protect_rows=map_draft_intel["our_protect_rows"],
        enemy_protect_rows=map_draft_intel["enemy_protect_rows"],
        participant_one_label=participant_one_label,
        participant_two_label=participant_two_label,
        is_tournament=False,
        back_to_maps_url=(url_for("team_detail", team_id=scrim.get("team_id")) + "#maps") if scrim.get("team_id") else url_for("teams"),
    )


@app.route("/teams/<int:team_id>/timelines/<path:map_name>")
def team_map_timeline(team_id: int, map_name: str):
    db = get_db()
    team_row = db.execute("SELECT id, name FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team_row is None:
        abort(404)

    source_scrims = get_team_history_scrims(team_row)
    participant_one_label = (team_row["name"] or "").strip() or "Your Team"
    participant_two_label = "All Opponents"

    map_timeline_row = None
    map_overview = {
        "maps": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0,
    }
    top_hero_rows: list[dict] = []
    enemy_top_hero_rows: list[dict] = []
    map_draft_intel = _collect_map_draft_intel(source_scrims, map_name)

    draft_timeline = build_draft_phase_timeline(source_scrims)
    map_timeline_row = next(
        (row for row in draft_timeline.get("maps", []) if row.get("map_name") == map_name),
        None,
    )

    our_hero_counts = defaultdict(int)
    our_hero_win_counts = defaultdict(int)
    enemy_hero_counts = defaultdict(int)
    enemy_hero_win_counts = defaultdict(int)
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
            is_enemy_win = result == "Loss"
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
                if is_enemy_win:
                    enemy_hero_win_counts[hero_name] += 1

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
            "win_rate": round((enemy_hero_win_counts.get(hero_name, 0) / hero_maps) * 100, 1) if hero_maps else 0,
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
        our_comp_rows=map_draft_intel["our_comp_rows"],
        enemy_comp_rows=map_draft_intel["enemy_comp_rows"],
        our_ban_rows=map_draft_intel["our_ban_rows"],
        enemy_ban_rows=map_draft_intel["enemy_ban_rows"],
        our_protect_rows=map_draft_intel["our_protect_rows"],
        enemy_protect_rows=map_draft_intel["enemy_protect_rows"],
        participant_one_label=participant_one_label,
        participant_two_label=participant_two_label,
        is_tournament=False,
        back_to_maps_url=url_for("team_detail", team_id=team_id) + "#maps",
    )


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/timelines/<path:map_name>")
def tournament_match_map_timeline(tournament_id: int, match_id: int, map_name: str):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)

    perspective = tournament_match.get("our_team_slot", "team1") if tournament_match.get("our_team_slot", "team1") in TEAM_SLOTS else "team1"
    source_scrims = build_tournament_match_scrims(tournament_record, perspective=perspective)
    map_draft_intel = _collect_map_draft_intel(source_scrims, map_name)
    draft_timeline = build_draft_phase_timeline(source_scrims)
    map_timeline_row = next(
        (row for row in draft_timeline.get("maps", []) if row.get("map_name") == map_name),
        None,
    )

    our_hero_counts = defaultdict(int)
    our_hero_win_counts = defaultdict(int)
    enemy_hero_counts = defaultdict(int)
    enemy_hero_win_counts = defaultdict(int)
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
            is_enemy_win = result == "Loss"
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
                if is_enemy_win:
                    enemy_hero_win_counts[hero_name] += 1

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
            "win_rate": round((enemy_hero_win_counts.get(hero_name, 0) / hero_maps) * 100, 1) if hero_maps else 0,
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
        our_comp_rows=map_draft_intel["our_comp_rows"],
        enemy_comp_rows=map_draft_intel["enemy_comp_rows"],
        our_ban_rows=map_draft_intel["our_ban_rows"],
        enemy_ban_rows=map_draft_intel["enemy_ban_rows"],
        our_protect_rows=map_draft_intel["our_protect_rows"],
        enemy_protect_rows=map_draft_intel["enemy_protect_rows"],
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
    update_map_type_from_form(map_entry)
    
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
        add_comp_section_endpoint="add_tournament_comp_section",
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
    update_map_type_from_form(map_entry)
    participant_one, participant_two = get_scrim_participants(scrim)
    valid_team_ids = {
        participant_one.get("id"),
        participant_two.get("id"),
    }

    side1_team_id = parse_team_id(request.form.get("map_team1_team_id", ""))
    updated_our_team_slot = map_entry.get("our_team_slot", "team1")
    if side1_team_id in valid_team_ids and participant_one.get("id") and participant_two.get("id"):
        updated_our_team_slot = "team1" if side1_team_id == participant_one.get("id") else "team2"
        map_entry["our_team_slot"] = updated_our_team_slot
    
    # Get team-specific scores and build combined score
    score_team1 = request.form.get("score_team1", "").strip()
    score_team2 = request.form.get("score_team2", "").strip()
    score = request.form.get("score", "").strip()
    our_atk = request.form.get("our_attack_score", "").strip()
    enemy_atk = request.form.get("enemy_attack_score", "").strip()
    is_attack_defense_map = map_entry.get("map_name") in ATTACK_DEFENSE_MAPS

    # Attack/defense score input takes priority for non-control maps
    if our_atk or enemy_atk:
        map_entry["our_attack_score"] = our_atk
        map_entry["enemy_attack_score"] = enemy_atk
        if updated_our_team_slot == "team1":
            map_entry["score"] = f"{our_atk}-{enemy_atk}"
        else:
            map_entry["score"] = f"{enemy_atk}-{our_atk}"
    elif is_attack_defense_map:
        if updated_our_team_slot == "team1":
            map_entry["our_attack_score"] = score_team1
            map_entry["enemy_attack_score"] = score_team2
        else:
            map_entry["our_attack_score"] = score_team2
            map_entry["enemy_attack_score"] = score_team1
        map_entry["score"] = build_score_text(score_team1, score_team2, score)
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
    update_map_type_from_form(map_entry)
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


@app.route("/draft-reasoner")
def draft_reasoner():
    # Keep legacy URL working, but route everyone into the unified Machine UI.
    return redirect(url_for("machine", tab="reasoner"))


@app.route("/machine")
def machine():
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    machine_history = SCRIMS + TOURNAMENT_MATCHES
    season_options = get_scrim_season_options(machine_history)
    default_season = get_current_season_from_recent_scrim(machine_history)
    has_unseasoned = any(not normalize_season_value(s.get("season", "")) for s in machine_history)
    return render_template(
        "machine.html",
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        teams=teams,
        maps=MAPS,
        season_options=season_options,
        default_season=default_season,
        has_unseasoned=has_unseasoned,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
    )


@app.route("/api/draft-reasoner/enemy-scouting")
def api_draft_reasoner_enemy_scouting():
    """Return tournament loss analysis for the enemy team.

    For each map where team_b lost to another team, aggregate:
    - bans the winning team used against team_b
    - heroes the winning team played on those maps
    - heroes team_b themselves played on those losing maps (shows what didn't work)
    Only includes season-filtered data when season is specified.
    """
    db = get_db()
    team_b_id = request.args.get("team_b", type=int)
    season_value = (request.args.get("season", "") or "").strip()
    if not team_b_id:
        return jsonify({"error": "team_b is required"}), 400

    team_b_row = db.execute("SELECT * FROM teams WHERE id = ?", (team_b_id,)).fetchone()
    if team_b_row is None:
        abort(404)

    team_id = int(team_b_row["id"])
    team_name = (team_b_row["name"] or "").strip().lower()

    # Counts indexed by hero name
    ban_counts: defaultdict[str, int] = defaultdict(int)   # bans by winning teams vs team_b
    winner_hero_counts: defaultdict[str, int] = defaultdict(int)  # heroes winning teams played
    loser_hero_counts: defaultdict[str, int] = defaultdict(int)   # heroes team_b played when losing
    loss_map_total = 0
    match_summaries: list[dict] = []

    for tournament_record in TOURNAMENT_MATCHES:
        # Season filter
        rec_season = normalize_season_value(tournament_record.get("season", ""))
        if season_value and season_value.lower() not in ("all", "") and rec_season:
            if season_value != rec_season:
                continue

        # Find team_b's entry in this tournament
        team_b_entry: dict | None = None
        for t_team in tournament_record.get("tournament_teams", []):
            if not isinstance(t_team, dict):
                continue
            src_id = t_team.get("source_team_id")
            t_name = (t_team.get("name") or "").strip().lower()
            if (isinstance(src_id, int) and src_id == team_id) or (not src_id and t_name and t_name == team_name):
                team_b_entry = t_team
                break

        if team_b_entry is None:
            continue

        b_t_id = team_b_entry.get("id")

        for match in tournament_record.get("matches", []):
            if not isinstance(match, dict):
                continue

            # Determine which slot team_b occupies
            if match.get("team1_tournament_team_id") == b_t_id:
                b_slot, w_slot = "team1", "team2"
                opponent_name = (match.get("team2_name") or "").strip() or "Opponent"
            elif match.get("team2_tournament_team_id") == b_t_id:
                b_slot, w_slot = "team2", "team1"
                opponent_name = (match.get("team1_name") or "").strip() or "Opponent"
            else:
                continue

            map_losses: list[str] = []
            for map_entry in match.get("maps", []):
                if not isinstance(map_entry, dict):
                    continue

                outcome = get_map_outcome_for_slot(map_entry, b_slot)
                if outcome != "Loss":
                    continue

                loss_map_total += 1
                map_name = (map_entry.get("map_name") or map_entry.get("map") or "").strip()
                if map_name:
                    map_losses.append(map_name)

                # Winning team's bans
                draft_data = map_entry.get("draft", {})
                if isinstance(draft_data, dict):
                    w_draft = draft_data.get(w_slot, {})
                    if isinstance(w_draft, dict):
                        for slot_key, hero_val in w_draft.items():
                            if slot_key.startswith("ban") and hero_val:
                                h = _canonical_draft_hero(hero_val)
                                if h:
                                    ban_counts[h] += 1

                # Winning team's heroes played
                for section in map_entry.get("comp", []):
                    if not isinstance(section, dict):
                        continue
                    for slot in section.get(w_slot, []):
                        if isinstance(slot, dict):
                            h = _canonical_draft_hero(slot.get("hero", ""))
                            if h:
                                winner_hero_counts[h] += 1
                    # Team_b heroes on losing maps
                    for slot in section.get(b_slot, []):
                        if isinstance(slot, dict):
                            h = _canonical_draft_hero(slot.get("hero", ""))
                            if h:
                                loser_hero_counts[h] += 1

            if map_losses:
                match_summaries.append({
                    "opponent": opponent_name,
                    "maps_lost": map_losses,
                    "count": len(map_losses),
                })

    match_summaries.sort(key=lambda r: r["count"], reverse=True)

    def _to_rows(counts: dict[str, int], total_maps: int) -> list[dict]:
        rows = [
            {
                "hero": h,
                "count": c,
                "rate": round((c / total_maps) * 100, 1) if total_maps else 0.0,
            }
            for h, c in counts.items()
        ]
        rows.sort(key=lambda r: r["count"], reverse=True)
        return rows[:15]

    return jsonify({
        "team_b_name": (team_b_row["name"] or "").strip(),
        "loss_maps_total": loss_map_total,
        "winning_team_bans": _to_rows(ban_counts, loss_map_total),
        "winning_team_heroes": _to_rows(winner_hero_counts, loss_map_total),
        "enemy_heroes_when_losing": _to_rows(loser_hero_counts, loss_map_total),
        "match_summaries": match_summaries[:10],
    })


@app.route("/api/draft-reasoner/model")
def api_draft_reasoner_model():
    """Return the full matchup model plus per-team comfort/ban/comp data for the reasoner."""
    db = get_db()
    team_a_id = request.args.get("team_a", type=int)
    team_b_id = request.args.get("team_b", type=int)
    if not team_a_id or not team_b_id:
        return jsonify({"error": "Both team_a and team_b are required."}), 400

    team_rows = db.execute(
        "SELECT * FROM teams WHERE id IN (?, ?)", (team_a_id, team_b_id)
    ).fetchall()
    team_lookup = {row["id"]: row for row in team_rows}
    team_a = team_lookup.get(team_a_id)
    team_b = team_lookup.get(team_b_id)
    if team_a is None or team_b is None:
        abort(404)

    season_value = request.args.get("season", "")
    selected_map_type = get_selected_map_type(request.args.get("map_type", "all"))
    selected_map_name = (request.args.get("map", "") or "").strip()
    include_scrims = _bool_arg("include_scrims", True)
    include_tournaments = _bool_arg("include_tournaments", True)

    def _get_filtered_scrims(team_row) -> tuple[list[dict], list[dict]]:
        all_scrims = get_team_history_for_sources(
            team_row,
            include_scrims=include_scrims,
            include_tournaments=include_tournaments,
        )
        season_options = get_scrim_season_options(all_scrims)
        default_season = get_current_season_from_recent_scrim(all_scrims)
        has_unseasoned = any(not normalize_season_value(s.get("season", "")) for s in all_scrims)
        effective_default = "all" if not season_value or season_value.lower() == "all" else default_season
        selected_season = get_selected_season(
            season_value,
            season_options,
            allow_unspecified=has_unseasoned,
            default_season=effective_default,
            strict=True,
        )
        scrims = filter_scrims_by_season(all_scrims, selected_season)
        scrims = filter_scrims_by_map_type(scrims, selected_map_type)
        hero_pool_scrims = filter_scrims_by_season(all_scrims, selected_season)
        hero_pool_scrims = filter_scrims_by_map_type(hero_pool_scrims, selected_map_type)
        if selected_map_name and selected_map_name.lower() != "all":
            filtered = []
            for scrim in scrims:
                scrim_copy = dict(scrim)
                scrim_copy["maps"] = [
                    m for m in scrim.get("maps", [])
                    if ((m.get("map") or m.get("map_name") or "").strip().lower() == selected_map_name.lower())
                ]
                if scrim_copy["maps"]:
                    filtered.append(scrim_copy)
            return filtered, hero_pool_scrims
        return scrims, hero_pool_scrims

    def _get_team_roster(team_id: int) -> list[dict]:
        rows = db.execute(
            """
            SELECT name, role, main_hero, COALESCE(is_sub, 0) AS is_sub
            FROM players
            WHERE team_id = ?
            ORDER BY COALESCE(is_sub, 0), name COLLATE NOCASE
            """,
            (team_id,),
        ).fetchall()
        return [
            {
                "name": (row["name"] or "").strip(),
                "role": (row["role"] or "").strip(),
                "main_hero": normalize_hero_slot_value(row["main_hero"] or ""),
                "is_sub": bool(row["is_sub"]),
            }
            for row in rows
            if (row["name"] or "").strip()
        ]

    a_scrims, a_hero_pool_scrims = _get_filtered_scrims(team_a)
    b_scrims, b_hero_pool_scrims = _get_filtered_scrims(team_b)
    matchup = build_matchup_tree_model(
        team_a["name"],
        a_scrims,
        team_b["name"],
        b_scrims,
        team_a_hero_pool_scrims=a_hero_pool_scrims,
        team_b_hero_pool_scrims=b_hero_pool_scrims,
    )
    teams_payload = matchup.get("teams", [])

    def _build_player_hero_rows(roster: list[dict], hero_pool_scrims: list[dict]) -> list[dict]:
        rows = []
        for player in roster:
            player_name = (player.get("name") or "").strip()
            if not player_name:
                continue
            bd = build_player_hero_map_breakdown(
                player_name,
                hero_pool_scrims,
                team_slots=TEAM_SLOTS,
                canonical_draft_hero=_canonical_draft_hero,
                get_map_outcome_for_slot=get_map_outcome_for_slot,
                map_modes=MAP_MODES,
                get_map_image_url=get_map_image_url,
            )
            hero_rows = bd.get("hero_rows", [])
            total_maps = round(sum(h.get("maps", 0) for h in hero_rows), 2)
            top_heroes = [
                {
                    "hero": h["hero"],
                    "maps": h["maps"],
                    "appearances": h["maps"],
                    "usage_rate": round((h["maps"] / total_maps) * 100, 1) if total_maps else 0.0,
                    "win_rate": h.get("win_rate", 0),
                }
                for h in hero_rows
            ]
            rows.append({
                "player": player_name,
                "maps": total_maps,
                "appearances": total_maps,
                "overall_win_rate": 0.0,
                "top_heroes": top_heroes,
            })
        rows.sort(key=lambda r: (r["maps"], r["player"].lower()), reverse=True)
        return rows

    if len(teams_payload) >= 1:
        roster_a = _get_team_roster(team_a_id)
        teams_payload[0]["roster_players"] = roster_a
        teams_payload[0].setdefault("model", {})["player_hero_rows"] = _build_player_hero_rows(roster_a, a_hero_pool_scrims)
    if len(teams_payload) >= 2:
        roster_b = _get_team_roster(team_b_id)
        teams_payload[1]["roster_players"] = roster_b
        teams_payload[1].setdefault("model", {})["player_hero_rows"] = _build_player_hero_rows(roster_b, b_hero_pool_scrims)
    return jsonify(matchup)


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
