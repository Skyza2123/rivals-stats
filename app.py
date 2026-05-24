import csv
import copy
import gzip
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
from typing import TYPE_CHECKING
from flask import Flask, render_template, request, redirect, url_for, abort, g, flash, jsonify, has_request_context, session, Response
from markupsafe import Markup
from werkzeug.datastructures import FileStorage
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
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

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000
_STATIC_ASSET_VERSION_CACHE: dict[str, tuple[tuple[int, int], str]] = {}

if TYPE_CHECKING:
    def init_db() -> None: ...
    def load_app_state() -> None: ...
    def schedule_draft_etl() -> None: ...
    def is_persistent_db_configured() -> bool: ...
    def normalize_hero_slot_value(raw_hero: str | None) -> str: ...
    def opposite_team_slot(team_slot: str) -> str: ...
    def _canonical_draft_hero(raw_hero: str) -> str: ...
    def build_scrim_analytics(
        scrims: list[dict],
        *,
        perspective_label: str = "Team",
        opponent_label: str = "Opponent",
        roster_player_names: list[str] | set[str] | None = None,
    ) -> dict: ...
    def build_draft_phase_timeline(scrims: list[dict]) -> dict: ...
    def split_score_pair(raw_score: str) -> tuple[str, str]: ...


def static_asset_url(filename: str) -> str:
    """Return a static asset URL with mtime-based cache busting."""
    version = "1"
    try:
        asset_path = Path(app.static_folder) / filename
        if asset_path.exists():
            cache_key = str(asset_path)
            stat = asset_path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = _STATIC_ASSET_VERSION_CACHE.get(cache_key)
            if cached and cached[0] == signature:
                version = cached[1]
            else:
                version = f"{stat.st_mtime_ns}-{stat.st_size}"
                _STATIC_ASSET_VERSION_CACHE[cache_key] = (signature, version)
    except OSError:
        pass
    return url_for("static", filename=filename, v=version)


@app.context_processor
def inject_static_asset_url() -> dict:
    return {"static_asset_url": static_asset_url}


@app.route("/favicon.ico")
def favicon_ico():
    return app.send_static_file("favicon.png")


@app.after_request
def compress_large_text_responses(response):
    """Gzip large dynamic text responses when the client supports it."""
    if (
        request.path.startswith("/static/")
        or request.path == "/favicon.ico"
        or response.direct_passthrough
        or response.status_code < 200
        or response.status_code >= 300
        or response.headers.get("Content-Encoding")
        or "gzip" not in request.headers.get("Accept-Encoding", "").lower()
    ):
        return response

    content_type = response.mimetype or ""
    if content_type == "text/event-stream" or not (
        content_type.startswith("text/")
        or content_type in {"application/json", "application/javascript", "application/xml"}
    ):
        return response

    payload = response.get_data()
    if len(payload) < 1024:
        return response

    compressed = gzip.compress(payload, compresslevel=5)
    if len(compressed) >= len(payload):
        return response

    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(compressed))
    response.headers.add("Vary", "Accept-Encoding")
    return response

# Serve static assets reliably behind WSGI hosts (Render/Gunicorn) when available.
_whitenoise_module = importlib.util.find_spec("whitenoise")
_enable_whitenoise = (
    (os.environ.get("RENDER") or "").strip().lower() == "true"
    or (os.environ.get("ENABLE_WHITENOISE") or "").strip().lower() in {"1", "true", "yes", "on"}
)
if _whitenoise_module is not None and _enable_whitenoise:
    from whitenoise import WhiteNoise

    app.wsgi_app = WhiteNoise(app.wsgi_app, root=str(Path(app.root_path) / "static"), prefix="static/")

# Trust X-Forwarded-Proto/Host headers from the reverse proxy (Render/Gunicorn)
# so Flask generates https:// URLs in redirects and url_for().
# ProxyFix must wrap the outermost middleware layer.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

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
PLAYER_ROLES = ["Vanguard", "Duelist", "Strategist", "Flex", "Coach", "AC", "Analyst"]
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
    "devildino": "Devil Dinosaur",
    "devildinosaur": "Devil Dinosaur",
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


MAP_IMAGE_CACHE_TTL_SECONDS = 60 * 60 * 24
_MAP_IMAGE_CACHE: dict[str, tuple[float, bytes, str]] = {}


def _canonical_map_name(map_name: str) -> str:
    if not map_name:
        return ""
    if map_name in MAP_IMAGES:
        return map_name
    return MAP_NAME_ALIASES.get(_normalize_map_name_key(map_name), map_name)


def _map_image_source_url(map_name: str) -> str:
    canonical_name = _canonical_map_name(map_name)
    if canonical_name:
        local_slug = re.sub(r"[^a-z0-9]+", "_", canonical_name.lower()).strip("_")
        local_candidates = [
            f"{local_slug}.jpg",
            f"{local_slug}.jpeg",
            f"{local_slug}.png",
            f"{local_slug}.webp",
            f"{local_slug}.svg",
        ]
        for filename in local_candidates:
            local_path = Path(app.static_folder) / "maps" / filename
            if local_path.exists():
                return f"/static/maps/{filename}"
    return MAP_IMAGES.get(canonical_name, "")


def _map_image_placeholder_svg(map_name: str) -> str:
    text = (map_name or "Map").strip()[:28] or "Map"
    return (
        "<svg xmlns='http://www.w3.org/2000/svg' width='960' height='360' viewBox='0 0 960 360'>"
        "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        "<stop offset='0%' stop-color='#0f172a'/><stop offset='60%' stop-color='#1e293b'/>"
        "<stop offset='100%' stop-color='#111827'/></linearGradient></defs>"
        "<rect width='960' height='360' fill='url(#g)'/>"
        f"<text x='480' y='190' text-anchor='middle' font-size='34' font-family='Arial, sans-serif' fill='#e6edf3'>{text}</text>"
        "</svg>"
    )


def get_map_image_url(map_name: str) -> str:
    if not map_name:
        return ""
    source_url = _map_image_source_url(map_name)
    if not source_url:
        return ""
    if source_url.startswith("/static/"):
        return source_url
    return f"/map-image/{quote(_canonical_map_name(map_name)[:80], safe='')}"


@app.route("/map-image/<path:map_name>")
def map_image_proxy(map_name: str):
    requested = (map_name or "").strip() or "Map"
    canonical_name = _canonical_map_name(requested) or requested
    source_url = _map_image_source_url(canonical_name)
    if source_url.startswith("/static/"):
        return redirect(source_url)

    now = time.time()
    cached = _MAP_IMAGE_CACHE.get(canonical_name)
    if cached and (now - cached[0]) < MAP_IMAGE_CACHE_TTL_SECONDS:
        return Response(
            cached[1],
            mimetype=cached[2],
            headers={"Cache-Control": "public, max-age=86400"},
        )

    if source_url:
        try:
            remote_request = Request(source_url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(remote_request, timeout=2.5) as remote_response:
                content_type = remote_response.headers.get_content_type() or "image/png"
                if content_type.startswith("image/"):
                    payload = remote_response.read()
                    if payload:
                        _MAP_IMAGE_CACHE[canonical_name] = (now, payload, content_type)
                        return Response(
                            payload,
                            mimetype=content_type,
                            headers={"Cache-Control": "public, max-age=86400"},
                        )
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            pass

    placeholder = _map_image_placeholder_svg(canonical_name).encode("utf-8")
    _MAP_IMAGE_CACHE[canonical_name] = (now, placeholder, "image/svg+xml")
    return Response(
        placeholder,
        mimetype="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )

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
RECENCY_HALFLIFE_DAYS = max(1.0, float(os.environ.get("MACHINE_RECENCY_HALFLIFE_DAYS", "7")))
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


_MODULE_PATHS = (
    "core/db_state.py",
    "core/auth_routes.py",
    "core/persistence.py",
    "core/scrim_history.py",
    "core/record_normalization.py",
    "core/player_stats.py",
    "core/imports.py",
    "core/tournaments.py",
    "core/map_forms.py",
    "core/media_helpers.py",
    "analytics/overview.py",
    "analytics/draft_timeline.py",
    "analytics/prep_plan.py",
    "analytics/draft_predictor.py",
    "analytics/opponent_tree.py",
    "analytics/matchup_tree.py",
    "analytics/hero_insights.py",
    "routes/dashboard_team_pages.py",
    "routes/team_tools_players.py",
    "routes/team_player_admin.py",
    "routes/enemy_admin.py",
    "routes/storage_admin.py",
    "routes/csv_imports.py",
    "routes/api_helpers.py",
    "routes/scrims_tournaments.py",
    "routes/timelines.py",
    "routes/map_edits.py",
    "routes/machine.py",
)

for _module_path in _MODULE_PATHS:
    _module_file = Path(__file__).parent / _module_path
    exec(compile(_module_file.read_text(encoding="utf-8"), str(_module_file), "exec"), globals())

try:
    init_db()
except Exception as e:
    app.logger.error(f"Failed to initialize database at startup: {type(e).__name__}: {e}")

try:
    load_app_state()
except Exception as e:
    app.logger.error(f"Failed to load app state at startup: {type(e).__name__}: {e}")

try:
    schedule_draft_etl()
except Exception as e:
    app.logger.error(f"Failed to schedule startup draft ETL: {type(e).__name__}: {e}")

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
