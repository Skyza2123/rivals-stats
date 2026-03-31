import csv
import copy
import io
import os
import json
import re
import hashlib
import sqlite3
import importlib
from difflib import SequenceMatcher
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
from flask import Flask, render_template, request, redirect, url_for, abort, g, flash, jsonify, has_request_context
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename
from data import (
    HEROES, HERO_ROLES, HERO_TRANSFORMATIONS, MAPS, MAP_IMAGES, MAP_SUBMAPS,
    SIDES, RESULTS, EVENT_TYPES, ATTACK_DEFENSE_MAPS, MAP_MODES,
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")

# Serve static assets reliably behind WSGI hosts (Render/Gunicorn) when available.
_whitenoise_module = importlib.util.find_spec("whitenoise")
if _whitenoise_module is not None:
    from whitenoise import WhiteNoise

    app.wsgi_app = WhiteNoise(app.wsgi_app, root=str(Path(app.root_path) / "static"), prefix="static/")

DB_PATH = Path(
    os.environ.get(
        "DATABASE_PATH",
        str(Path(app.root_path) / "rivals_stats.db"),
    )
)
TEAM_LOGO_DIR = Path(app.static_folder) / "uploads" / "team_logos"
PLAYER_ROLES = ["Vanguard", "Duelist", "Strategist", "Flex"]
TEAM_SLOTS = ["team1", "team2"]
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
    "team1_ban4",
    "team2_protect2",
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
MAX_SCRIM_BACKUPS = 100


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
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                notes TEXT NOT NULL DEFAULT '',
                logo_path TEXT NOT NULL DEFAULT '',
                is_personal INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
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
        ):
            conn.execute(
                "INSERT OR IGNORE INTO app_state (state_key, state_value) VALUES (?, ?)",
                (key, default),
            )

        team_columns = {row[1] for row in conn.execute("PRAGMA table_info(teams)").fetchall()}
        if "logo_path" not in team_columns:
            conn.execute("ALTER TABLE teams ADD COLUMN logo_path TEXT NOT NULL DEFAULT ''")
        if "is_personal" not in team_columns:
            conn.execute("ALTER TABLE teams ADD COLUMN is_personal INTEGER NOT NULL DEFAULT 0")

        enemy_team_columns = {row[1] for row in conn.execute("PRAGMA table_info(enemy_teams)").fetchall()}
        if "logo_path" not in enemy_team_columns:
            conn.execute("ALTER TABLE enemy_teams ADD COLUMN logo_path TEXT NOT NULL DEFAULT ''")

        TEAM_LOGO_DIR.mkdir(parents=True, exist_ok=True)
        conn.commit()
    finally:
        conn.close()


def load_app_state() -> None:
    global SCRIMS, TOURNAMENT_MATCHES, NEXT_SCRIM_ID, NEXT_TOURNAMENT_ID, NEXT_MAP_ID, NEXT_EVENT_ID, LAST_SCRIMS_REV, LAST_SCRIMS_ETAG
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
    finally:
        conn.close()


@app.before_request
def refresh_app_state_from_db() -> None:
    # Keep in-memory state in sync across hosted worker processes.
    load_app_state()


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


def normalize_season_value(raw_value: str) -> str:
    return " ".join((raw_value or "").strip().split())


def normalize_match_team_slot(raw_value: str) -> str:
    value = str(raw_value or "").strip()
    return value if value in TEAM_SLOTS else "team1"


def get_scrim_participant_labels(scrim: dict) -> tuple[str, str]:
    our_label = str(scrim.get("team_name", "")).strip() or "Your Team"
    enemy_label = str(scrim.get("enemy_team") or scrim.get("opponent") or "").strip() or "Enemy Team"
    if normalize_match_team_slot(scrim.get("team_slot", "team1")) == "team2":
        return enemy_label, our_label
    return our_label, enemy_label


def normalize_scrim_record(scrim: dict) -> dict:
    scrim["season"] = normalize_season_value(scrim.get("season", ""))
    scrim["team_slot"] = normalize_match_team_slot(scrim.get("team_slot", "team1"))
    if not scrim.get("enemy_team") and scrim.get("opponent"):
        scrim["enemy_team"] = scrim.get("opponent", "")
    if not scrim.get("opponent") and scrim.get("enemy_team"):
        scrim["opponent"] = scrim.get("enemy_team", "")
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
    match["team1_players"] = [str(player).strip() for player in match.get("team1_players", []) if str(player).strip()]
    match["team2_players"] = [str(player).strip() for player in match.get("team2_players", []) if str(player).strip()]

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
        players = [str(player).strip() for player in team.get("players", []) if str(player).strip()]
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
    events_mentioned = 0
    target_lower = target.lower()
    exact_name_pattern = re.compile(r"(?<!\\w)" + re.escape(target_lower) + r"(?!\\w)")
    source_scrims = scrims if scrims is not None else SCRIMS

    for scrim in source_scrims:
        for map_entry in scrim["maps"]:
            player_found = False
            for section in map_entry.get("comp", []):
                for team in ("team1", "team2"):
                    for slot in section.get(team, []):
                        if slot.get("player", "").strip().lower() == target_lower:
                            player_found = True
                            break
                    if player_found:
                        break
                if player_found:
                    break

            if player_found:
                maps_played += 1
                if map_entry.get("result") == "Win":
                    wins += 1
                elif map_entry.get("result") == "Loss":
                    losses += 1

            for event in map_entry.get("events", []):
                description = event.get("description", "").strip().lower()
                if exact_name_pattern.search(description):
                    events_mentioned += 1

    win_rate = round((wins / maps_played) * 100, 1) if maps_played else 0

    return {
        "maps_played": maps_played,
        "wins": wins,
        "losses": losses,
        "events_mentioned": events_mentioned,
        "win_rate": win_rate,
    }


def build_player_hero_map_breakdown(player_name: str, scrims: list[dict]) -> dict:
    target_name = (player_name or "").strip().lower()
    if not target_name:
        return {
            "hero_rows": [],
            "map_rows": [],
        }

    hero_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})
    map_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            player_found = False
            heroes_for_player_in_map = set()
            for section in map_entry.get("comp", []):
                for slot in section.get(our_team_slot, []):
                    if (slot.get("player", "") or "").strip().lower() != target_name:
                        continue
                    player_found = True
                    hero_name = _canonical_draft_hero(slot.get("hero", ""))
                    if hero_name:
                        heroes_for_player_in_map.add(hero_name)

            if not player_found:
                continue

            map_name = (map_entry.get("map_name", "") or "").strip()
            result = map_entry.get("result", "")

            if map_name:
                map_stats[map_name]["maps"] += 1
                if result == "Win":
                    map_stats[map_name]["wins"] += 1
                elif result == "Loss":
                    map_stats[map_name]["losses"] += 1

            for hero_name in heroes_for_player_in_map:
                hero_stats[hero_name]["maps"] += 1
                if result == "Win":
                    hero_stats[hero_name]["wins"] += 1
                elif result == "Loss":
                    hero_stats[hero_name]["losses"] += 1

    hero_rows = []
    for hero_name, stats in hero_stats.items():
        maps_played = stats["maps"]
        hero_rows.append(
            {
                "hero": hero_name,
                "maps": maps_played,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round((stats["wins"] / maps_played) * 100, 1) if maps_played else 0,
            }
        )
    hero_rows.sort(key=lambda row: (row["maps"], row["win_rate"]), reverse=True)

    map_rows = []
    for map_name, stats in map_stats.items():
        maps_played = stats["maps"]
        map_rows.append(
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
    map_rows.sort(key=lambda row: (row["maps"], row["win_rate"]), reverse=True)

    return {
        "hero_rows": hero_rows,
        "map_rows": map_rows,
    }


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


def parse_name_list(raw: str) -> list[str]:
    parts = re.split(r"[\r\n,]+", raw or "")
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = part.strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    return cleaned


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
            result = get_result_for_slot(map_entry, team_slot)
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
    hero_text = (raw_hero or "").strip()
    if not hero_text:
        return ""
    return _resolve_hero_transform_key(hero_text) or hero_text


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

    team_players = []
    enemy_players = []
    enemy_team_data = None
    team1_label = match_record.get("team_name") or match_record.get("team1_name") or "Team 1"
    team2_label = match_record.get("enemy_team") or match_record.get("opponent") or match_record.get("team2_name") or "Team 2"
    picked_by_label = ""

    db = get_db()
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
        enemy_players = [
            {
                "name": player_name,
                "role": "",
                "main_hero": "",
            }
            for player_name in (team2 or {}).get("players", [])
        ]
    else:
        team1_label, team2_label = get_scrim_participant_labels(match_record)
        map_entry["team1_name"] = team1_label
        map_entry["team2_name"] = team2_label
        team_id = match_record.get("team_id")
        if team_id:
            player_rows = db.execute(
                "SELECT name FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
                (team_id,),
            ).fetchall()
            team_players = [row["name"] for row in player_rows]

        enemy_team_id = match_record.get("enemy_team_id")
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

    return {
        "match_record": match_record,
        "map_entry": map_entry,
        "heroes": HEROES,
        "hero_roles": HERO_ROLES,
        "hero_transformations": HERO_TRANSFORMATIONS,
        "map_images": MAP_IMAGES,
        "map_submaps": MAP_SUBMAPS,
        "maps": MAPS,
        "sides": SIDES,
        "results": RESULTS,
        "event_types": EVENT_TYPES,
        "team_players": team_players,
        "enemy_team": enemy_team_data,
        "enemy_players": enemy_players,
        "team1_label": team1_label,
        "team2_label": team2_label,
        "picked_by_label": picked_by_label,
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
    return f"uploads/team_logos/{filename}"


def delete_team_logo_file(relative_path: str) -> None:
    if not relative_path:
        return
    logo_path = Path(app.static_folder) / relative_path
    try:
        if logo_path.exists() and logo_path.is_file():
            logo_path.unlink()
    except OSError:
        # Failing to remove an old logo file should not block team updates.
        pass


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
            "totals": {"ban3": 0, "protect2": 0, "ban4": 0},
        }
    )
    protect1_influence_targets = defaultdict(
        lambda: {
            "ban2": defaultdict(int),
            "ban3": defaultdict(int),
            "protect2": defaultdict(int),
            "ban4": defaultdict(int),
            "totals": {"ban2": 0, "ban3": 0, "protect2": 0, "ban4": 0},
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
            is_win = map_entry.get("result") == "Win"
            is_loss = map_entry.get("result") == "Loss"
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

            if our_protect_slots.get("protect2"):
                for slot in ("ban1", "ban2", "ban3"):
                    if our_ban_slots.get(slot):
                        ban_to_protect_pairs[our_ban_slots[slot]][our_protect_slots["protect2"]] += 1

            ban2_hero = our_ban_slots.get("ban2", "")
            if ban2_hero:
                ban3_hero = our_ban_slots.get("ban3", "")
                protect2_hero = our_protect_slots.get("protect2", "")
                ban4_hero = our_ban_slots.get("ban4", "")
                if ban3_hero:
                    second_order_ban_targets[ban2_hero]["ban3"][ban3_hero] += 1
                    second_order_ban_targets[ban2_hero]["totals"]["ban3"] += 1
                if protect2_hero:
                    second_order_ban_targets[ban2_hero]["protect2"][protect2_hero] += 1
                    second_order_ban_targets[ban2_hero]["totals"]["protect2"] += 1
                if ban4_hero:
                    second_order_ban_targets[ban2_hero]["ban4"][ban4_hero] += 1
                    second_order_ban_targets[ban2_hero]["totals"]["ban4"] += 1

            protect1_hero = our_protect_slots.get("protect1", "")
            if protect1_hero:
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
                if shared_draft_heroes >= 6:
                    draft_hard_mirror_count += 1
                elif shared_draft_heroes >= 3:
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

            heroes_in_map = set()
            comp_profiles_in_map = set()
            enemy_comp_profiles_in_map = set()
            for section in map_entry.get("comp", []):
                for slot in section.get(our_team_slot, []):
                    hero_name = (slot.get("hero", "") or "").strip()
                    if hero_name:
                        heroes_in_map.add(hero_name)

                section_heroes = [
                    (_resolve_hero_transform_key((slot.get("hero", "") or "").strip()) or (slot.get("hero", "") or "").strip())
                    for slot in section.get(our_team_slot, [])
                    if (slot.get("hero", "") or "").strip()
                ]
                if section_heroes:
                    comp_profiles_in_map.add(classify_comp_profile(section_heroes))

                enemy_section_heroes = [
                    (_resolve_hero_transform_key((slot.get("hero", "") or "").strip()) or (slot.get("hero", "") or "").strip())
                    for slot in section.get(opposite_team_slot(our_team_slot), [])
                    if (slot.get("hero", "") or "").strip()
                ]
                if enemy_section_heroes:
                    enemy_comp_profiles_in_map.add(classify_comp_profile(enemy_section_heroes))

                if section_heroes and enemy_section_heroes:
                    shared_comp_heroes = len(set(section_heroes) & set(enemy_section_heroes))
                    comp_mirror_total += 1
                    if shared_comp_heroes >= 6:
                        comp_hard_mirror_count += 1
                    elif shared_comp_heroes >= 3:
                        comp_soft_mirror_count += 1

            canonical_heroes_in_map = {
                canonical_hero(hero_name)
                for hero_name in heroes_in_map
                if canonical_hero(hero_name)
            }
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
                else:
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

            for hero_name in heroes_in_map:
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

    second_order_ban_rows = []
    for ban2_hero, target_data in second_order_ban_targets.items():
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
                "ban2_hero": ban2_hero,
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

    protect1_influence_rows = []
    for protect1_hero, target_data in protect1_influence_targets.items():
        ban2_top = top_slot_pick(target_data["ban2"], target_data["totals"]["ban2"])
        ban3_top = top_slot_pick(target_data["ban3"], target_data["totals"]["ban3"])
        protect2_top = top_slot_pick(target_data["protect2"], target_data["totals"]["protect2"])
        ban4_top = top_slot_pick(target_data["ban4"], target_data["totals"]["ban4"])
        protect1_influence_rows.append(
            {
                "protect1_hero": protect1_hero,
                "ban2": ban2_top,
                "ban3": ban3_top,
                "protect2": protect2_top,
                "ban4": ban4_top,
                "sample_total": sum(target_data["totals"].values()),
            }
        )
    protect1_influence_rows.sort(key=lambda row: row["sample_total"], reverse=True)

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
        if not open_maps:
            continue

        played_when_open = stats["played_when_open"]
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
            }
        )
    hero_open_rows.sort(
        key=lambda row: (
            row["open_maps"],
            row["play_when_open_rate"],
            row["played_when_open"],
        ),
        reverse=True,
    )

    mirror_rates = {
        "draft": {
            "samples": draft_mirror_total,
            "soft_count": draft_soft_mirror_count,
            "hard_count": draft_hard_mirror_count,
            "soft_rate": pct(draft_soft_mirror_count, draft_mirror_total),
            "hard_rate": pct(draft_hard_mirror_count, draft_mirror_total),
            "any_mirror_rate": pct(draft_soft_mirror_count + draft_hard_mirror_count, draft_mirror_total),
        },
        "comp": {
            "samples": comp_mirror_total,
            "soft_count": comp_soft_mirror_count,
            "hard_count": comp_hard_mirror_count,
            "soft_rate": pct(comp_soft_mirror_count, comp_mirror_total),
            "hard_rate": pct(comp_hard_mirror_count, comp_mirror_total),
            "any_mirror_rate": pct(comp_soft_mirror_count + comp_hard_mirror_count, comp_mirror_total),
        },
    }

    triple_support_count = comp_profile_stats["triple_support"]["count"]
    two_two_two_count = comp_profile_stats["two_two_two"]["count"]
    triple_support_rate = pct(triple_support_count, total_maps)
    two_two_two_rate = pct(two_two_two_count, total_maps)
    comp_difference_rate = round(triple_support_rate - two_two_two_rate, 1)
    triple_support_win_rate = pct(comp_profile_stats["triple_support"]["wins"], triple_support_count)
    two_two_two_win_rate = pct(comp_profile_stats["two_two_two"]["wins"], two_two_two_count)
    enemy_triple_support_count = enemy_comp_profile_stats["triple_support"]["count"]
    enemy_triple_support_rate = pct(enemy_triple_support_count, total_maps)
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
    hero_text = (raw_hero or "").strip()
    if not hero_text:
        return ""
    return _resolve_hero_transform_key(hero_text) or hero_text


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
        scrim_enemy_id = scrim.get("enemy_team_id")
        if enemy_team_id and scrim_enemy_id == enemy_team_id:
            filtered.append(scrim)
            continue

        scrim_enemy_name = (scrim.get("enemy_team") or scrim.get("opponent") or "").strip().lower()
        if enemy_name_lower and scrim_enemy_name == enemy_name_lower:
            filtered.append(scrim)

    return filtered


def build_prep_expected_comp_plan(prep_scrims: list[dict], team_players: list[sqlite3.Row | dict], prep_analytics: dict) -> dict:
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

    player_by_main_hero = defaultdict(list)
    for player in players:
        main_hero = _canonical_draft_hero(player["main_hero"])
        if main_hero:
            player_by_main_hero[main_hero].append(player)

    for scrim in prep_scrims:
        for map_entry in scrim.get("maps", []):
            result = map_entry.get("result", "")
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            map_pairs = set()
            map_heroes = set()
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
                    map_heroes.add(hero_name)

                    player_name = (slot.get("player", "") or "").strip()
                    if player_name:
                        map_pairs.add((hero_name, player_name))

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
                lineup_key = tuple(sorted(set(largest_lineup)))
                comp_variant_counts[lineup_key]["count"] += 1
                if result == "Win":
                    comp_variant_counts[lineup_key]["wins"] += 1
                elif result == "Loss":
                    comp_variant_counts[lineup_key]["losses"] += 1

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

    expected_comp_variants = []
    sorted_variants = sorted(comp_variant_counts.items(), key=lambda row: (row[1]["count"], row[1]["wins"]), reverse=True)
    for idx, (heroes, stats) in enumerate(sorted_variants[:3]):
        used_variant_names = set()
        slots = []
        for hero_name in heroes:
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

        expected_comp_variants.append(
            {
                "label": f"Expected Comp {idx + 1}",
                "heroes": slots,
                "maps": stats["count"],
                "win_rate": round((stats["wins"] / stats["count"]) * 100, 1) if stats["count"] else 0,
            }
        )

    top_enemy_bans = prep_analytics.get("enemy_ban_rows", [])[:4]
    suggested_adjustments = []
    banned_set = {_canonical_draft_hero(row.get("hero", "")) for row in top_enemy_bans}
    fallback_pool = [item for item in expected_core if _canonical_draft_hero(item["hero"]) not in banned_set]
    for row in top_enemy_bans:
        banned_hero = row.get("hero", "")
        banned_key = _canonical_draft_hero(banned_hero)
        impacted_slot = next((item for item in expected_core if _canonical_draft_hero(item["hero"]) == banned_key), None)

        replacement = None
        if fallback_pool:
            replacement = fallback_pool[0]
            fallback_pool = fallback_pool[1:] + fallback_pool[:1]

        suggested_adjustments.append(
            {
                "banned_hero": banned_hero,
                "ban_rate": row.get("ban_rate", 0),
                "impacted_player_name": impacted_slot.get("player", {}).get("name") if impacted_slot else "TBD",
                "replacement_hero": replacement.get("hero") if replacement else "TBD",
                "replacement_player_name": replacement.get("player", {}).get("name") if replacement else "TBD",
            }
        )

    return {
        "expected_core": expected_core,
        "expected_comp_variants": expected_comp_variants,
        "suggested_adjustments": suggested_adjustments,
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
    enemy_lookup = {str(enemy["id"]): enemy for enemy in enemy_teams}
    selected_enemy_id = selected_enemy_id_raw.strip()
    selected_enemy = enemy_lookup.get(selected_enemy_id)

    prep_scrims = team_scrims
    selected_enemy_name = ""
    if selected_enemy is not None:
        selected_enemy_name = selected_enemy["name"]
        prep_scrims = filter_team_scrims_for_enemy(team_scrims, int(selected_enemy_id), selected_enemy_name)

    prep_analytics = build_scrim_analytics(prep_scrims)
    draft_phase_timeline = build_draft_phase_timeline(prep_scrims)
    prep_expected_plan = build_prep_expected_comp_plan(prep_scrims, team_players, prep_analytics)

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
    map_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})
    mode_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})
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
    sections_played = 0
    timeline_points = []

    sorted_scrims = sorted(team_scrims, key=lambda s: (s.get("scrim_date", ""), s.get("id", 0)))

    for scrim in sorted_scrims:
        scrim_maps = 0
        scrim_wins = 0
        scrim_losses = 0

        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            map_has_hero = False
            map_teammates = set()
            map_duo_partners = set()
            map_comp_signatures = set()
            for section in map_entry.get("comp", []):
                raw_heroes = [
                    (slot.get("hero") or "").strip()
                    for slot in section.get(our_team_slot, [])
                    if (slot.get("hero") or "").strip()
                ]
                our_heroes = [(_resolve_hero_transform_key(h) or h) for h in raw_heroes]
                if not our_heroes:
                    continue

                if not any(_hero_match_key(h) == target_key for h in our_heroes):
                    continue

                map_has_hero = True
                teammates = {h for h in our_heroes if _hero_match_key(h) != target_key}
                map_teammates.update(teammates)

                if target_role:
                    same_role_partners = {
                        h
                        for h in our_heroes
                        if _hero_match_key(h) != target_key and _hero_role(h) == target_role
                    }
                    map_duo_partners.update(same_role_partners)

                # Track unique full-lineup comps once per map.
                comp_signature = tuple(sorted(set(our_heroes)))
                if comp_signature:
                    map_comp_signatures.add(comp_signature)

            if not map_has_hero:
                continue

            # Count hero appearance once per map, regardless of attack/defense split.
            sections_played += 1
            for teammate in map_teammates:
                ally_stats[teammate]["count"] += 1
                if map_entry.get("result") == "Win":
                    ally_stats[teammate]["wins"] += 1
                elif map_entry.get("result") == "Loss":
                    ally_stats[teammate]["losses"] += 1

            for duo_partner in map_duo_partners:
                duo_stats[duo_partner]["count"] += 1
                if map_entry.get("result") == "Win":
                    duo_stats[duo_partner]["wins"] += 1
                elif map_entry.get("result") == "Loss":
                    duo_stats[duo_partner]["losses"] += 1

            for comp_signature in map_comp_signatures:
                comp_stats[comp_signature]["count"] += 1
                if map_entry.get("result") == "Win":
                    comp_stats[comp_signature]["wins"] += 1
                elif map_entry.get("result") == "Loss":
                    comp_stats[comp_signature]["losses"] += 1

            map_name = (map_entry.get("map_name") or "").strip()
            if map_name:
                map_stats[map_name]["maps"] += 1

            total_maps += 1
            scrim_maps += 1
            if map_entry.get("result") == "Win":
                total_wins += 1
                scrim_wins += 1
                if map_name:
                    map_stats[map_name]["wins"] += 1
            elif map_entry.get("result") == "Loss":
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

    return {
        "hero": display_name,
        "target_role": target_role,
        "hero_image_url": _hero_image_url(display_name),
        "summary": {
            "maps_played": total_maps,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate": round((total_wins / total_maps) * 100, 1) if total_maps else 0,
            "sections_played": sections_played,
            "unique_allies": len(ally_rows),
        },
        "ally_rows": ally_rows,
        "duo_rows": duo_rows,
        "comp_rows": comp_rows,
        "map_rows": map_rows,
        "timeline_points": timeline_points,
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

        hero_map_counts = {hero: 0 for hero in top_heroes}
        for map_entry in maps:
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            map_heroes = set()
            for section in map_entry.get("comp", []):
                for slot in section.get(our_team_slot, []):
                    hero_name = (slot.get("hero") or "").strip()
                    if not hero_name:
                        continue
                    resolved = _resolve_hero_transform_key(hero_name) or hero_name
                    map_heroes.add(resolved)

            for hero in top_heroes:
                if hero in map_heroes:
                    hero_map_counts[hero] += 1

        labels.append(f"{scrim.get('scrim_date', '')} vs {scrim.get('enemy_team') or scrim.get('opponent') or 'Unknown'}")
        for hero in top_heroes:
            usage_rate = round((hero_map_counts[hero] / map_count) * 100, 1) if map_count else 0
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
    )


@app.route("/teams")
def teams():
    db = get_db()
    team_rows = db.execute(
        """
        SELECT t.id, t.name, t.notes, t.logo_path, t.is_personal, COUNT(p.id) AS player_count
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
        all_team_scrims = [
            scrim
            for scrim in SCRIMS
            if scrim.get("team_id") == row["id"]
            or (
                not scrim.get("team_id")
                and (scrim.get("team_name", "") or "").strip().lower() == row["name"].strip().lower()
            )
        ]
        team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
        team_maps = sum(len(scrim.get("maps", [])) for scrim in team_scrims)
        team_wins = sum(
            1
            for scrim in team_scrims
            for map_entry in scrim.get("maps", [])
            if map_entry.get("result") == "Win"
        )
        team_win_rate = round((team_wins / team_maps) * 100, 1) if team_maps else 0

        teams_with_scrim_stats.append(
            {
                "id": row["id"],
                "name": row["name"],
                "notes": row["notes"],
                "logo_path": row["logo_path"],
                "is_personal": bool(row["is_personal"]),
                "player_count": row["player_count"],
                "scrim_count": len(team_scrims),
                "map_count": team_maps,
                "map_win_rate": team_win_rate,
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

    def load_team_payload(team_row: dict | None) -> dict | None:
        if team_row is None:
            return None

        scrim_pool = [
            scrim
            for scrim in SCRIMS
            if scrim.get("team_id") == team_row["id"]
            or (
                not scrim.get("team_id")
                and (scrim.get("team_name", "") or "").strip().lower() == team_row["name"].strip().lower()
            )
        ]
        tournament_pool = build_team_tournament_scrims(team_row)
        team_scrims = tournament_pool if selected_mode == "tournaments" else scrim_pool
        analytics = build_scrim_analytics(team_scrims)
        return {
            "team": team_row,
            "analytics": analytics,
            "top_heroes": analytics.get("hero_rows", [])[:6],
            "top_maps": analytics.get("map_rows", [])[:6],
        }

    team_a = load_team_payload(team_lookup.get(selected_team_a_id))
    team_b = load_team_payload(team_lookup.get(selected_team_b_id))

    return render_template(
        "teams_compare.html",
        team_options=team_options,
        selected_team_a_id=selected_team_a_id,
        selected_team_b_id=selected_team_b_id,
        selected_mode=selected_mode,
        team_a=team_a,
        team_b=team_b,
    )


@app.route("/teams/create", methods=["POST"])
def create_team():
    db = get_db()
    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    logo_path = save_team_logo(request.files.get("logo"), name)
    is_personal = 1 if request.form.get("is_personal", "").strip() == "1" else 0

    if not name:
        flash("Team name is required.", "error")
        return redirect(url_for("teams"))

    try:
        db.execute(
            "INSERT INTO teams (name, notes, logo_path, is_personal) VALUES (?, ?, ?, ?)",
            (name, notes, logo_path, is_personal),
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

    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
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
            "UPDATE teams SET name = ?, notes = ?, logo_path = ?, is_personal = ? WHERE id = ?",
            (name, notes, logo_path, is_personal, team_id),
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


@app.route("/teams/<int:team_id>")
def team_detail(team_id: int):
    db = get_db()
    migrate_enemy_teams_to_team_database(db)
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    all_team_scrims = [
        scrim
        for scrim in SCRIMS
        if scrim.get("team_id") == team["id"]
        or (
            not scrim.get("team_id")
            and (scrim.get("team_name", "") or "").strip().lower() == team["name"].strip().lower()
        )
    ]
    season_options = get_scrim_season_options(all_team_scrims)
    default_season = get_current_season_from_recent_scrim(all_team_scrims)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in all_team_scrims)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )
    team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
    predictor_inputs = {
        field_key: (request.args.get(field_key) or "").strip()
        for field_key in PREDICTOR_INPUT_ORDER
    }
    draft_predictor = build_draft_predictor(team_scrims, predictor_inputs)
    team_tournament_rows = build_team_tournament_rows(team)

    team_analytics = build_scrim_analytics(team_scrims)
    hero_graph_rows = [
        {
            "hero": row["hero"],
            "maps": row["maps"],
            "win_rate": row["win_rate"],
            "usage_rate": round((row["maps"] / team_analytics["summary"]["total_maps"]) * 100, 1)
            if team_analytics["summary"]["total_maps"]
            else 0,
        }
        for row in team_analytics.get("hero_rows", [])
    ]
    hero_usage_timeline = build_hero_usage_timeline(
        team_scrims,
        [row["hero"] for row in hero_graph_rows[:6]],
    )

    map_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})
    mode_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})

    for scrim in team_scrims:
        for map_entry in scrim.get("maps", []):
            map_name = (map_entry.get("map_name", "") or "").strip()
            if not map_name:
                continue

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

    player_rows = db.execute(
        "SELECT * FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
        (team_id,),
    ).fetchall()

    players = []
    for row in player_rows:
        stats = compute_player_stats(row["name"], team_scrims)
        players.append({
            "id": row["id"],
            "name": row["name"],
            "role": row["role"],
            "main_hero": row["main_hero"],
            "notes": row["notes"],
            "stats": stats,
        })

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
        hero_graph_rows=hero_graph_rows,
        hero_usage_timeline=hero_usage_timeline,
        team_scrim_count=len(team_scrims),
        team_scrim_total_count=len(all_team_scrims),
        team_map_cards=team_map_cards,
        team_map_mode_rows=team_map_mode_rows,
        best_mode=best_mode,
        worst_mode=worst_mode,
        map_modes=MAP_MODES,
        map_images=MAP_IMAGES,
        draft_predictor=draft_predictor,
        hero_transformations=HERO_TRANSFORMATIONS,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
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

            result = get_result_for_slot(map_entry, team_slot)
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

    all_team_scrims = [
        scrim
        for scrim in SCRIMS
        if scrim.get("team_id") == team["id"]
        or (
            not scrim.get("team_id")
            and (scrim.get("team_name", "") or "").strip().lower() == team["name"].strip().lower()
        )
    ]
    season_options = get_scrim_season_options(all_team_scrims)
    default_season = get_current_season_from_recent_scrim(all_team_scrims)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in all_team_scrims)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )
    team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)

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
        **prep_context,
    )


@app.route("/teams/<int:team_id>/draft-predict")
def team_draft_predict(team_id: int):
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    all_team_scrims = [
        scrim
        for scrim in SCRIMS
        if scrim.get("team_id") == team["id"]
        or (
            not scrim.get("team_id")
            and (scrim.get("team_name", "") or "").strip().lower() == team["name"].strip().lower()
        )
    ]
    season_options = get_scrim_season_options(all_team_scrims)
    default_season = get_current_season_from_recent_scrim(all_team_scrims)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in all_team_scrims)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )
    team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
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

    team_scrims = [
        scrim
        for scrim in SCRIMS
        if scrim.get("team_id") == team["id"]
        or (
            not scrim.get("team_id")
            and (scrim.get("team_name", "") or "").strip().lower() == team["name"].strip().lower()
        )
    ]

    hero_insights = build_team_hero_insights(team_scrims, target_hero)
    if not hero_insights["summary"]["maps_played"]:
        flash(f"No comp data found for {target_hero}.", "error")
        return redirect(url_for("team_detail", team_id=team_id) + "#comps")

    return render_template(
        "hero_detail.html",
        team=team,
        hero_insights=hero_insights,
        map_images=MAP_IMAGES,
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

    all_team_scrims = [
        scrim
        for scrim in SCRIMS
        if scrim.get("team_id") == team["id"]
        or (
            not scrim.get("team_id")
            and (scrim.get("team_name", "") or "").strip().lower() == team["name"].strip().lower()
        )
    ]
    season_options = get_scrim_season_options(all_team_scrims)
    default_season = get_current_season_from_recent_scrim(all_team_scrims)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in all_team_scrims)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )
    team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)

    player_stats = compute_player_stats(player["name"], team_scrims)
    breakdown = build_player_hero_map_breakdown(player["name"], team_scrims)

    return render_template(
        "player_detail.html",
        team=team,
        player=player,
        player_stats=player_stats,
        player_hero_rows=breakdown["hero_rows"],
        player_map_rows=breakdown["map_rows"],
        selected_season=selected_season,
        season_options=season_options,
        has_unseasoned_scrims=has_unseasoned_scrims,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
    )


@app.route("/players/compare")
def player_compare():
    db = get_db()
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

    def load_player_payload(player_row: dict | None) -> dict | None:
        if player_row is None:
            return None

        team_scrims = [
            scrim
            for scrim in SCRIMS
            if scrim.get("team_id") == player_row["team_id"]
            or (
                not scrim.get("team_id")
                and (scrim.get("team_name", "") or "").strip().lower() == (player_row.get("team_name", "") or "").strip().lower()
            )
        ]
        stats = compute_player_stats(player_row["name"], team_scrims)
        breakdown = build_player_hero_map_breakdown(player_row["name"], team_scrims)
        return {
            "profile": player_row,
            "stats": stats,
            "hero_rows": breakdown["hero_rows"][:8],
            "map_rows": breakdown["map_rows"][:8],
        }

    payload_a = load_player_payload(player_a)
    payload_b = load_player_payload(player_b)

    shared_heroes = []
    shared_maps = []
    if payload_a and payload_b:
        hero_lookup_a = {row["hero"]: row for row in payload_a["hero_rows"]}
        hero_lookup_b = {row["hero"]: row for row in payload_b["hero_rows"]}
        for hero_name in sorted(set(hero_lookup_a) & set(hero_lookup_b)):
            shared_heroes.append(
                {
                    "hero": hero_name,
                    "player_a_maps": hero_lookup_a[hero_name]["maps"],
                    "player_b_maps": hero_lookup_b[hero_name]["maps"],
                }
            )
        shared_heroes.sort(key=lambda row: row["player_a_maps"] + row["player_b_maps"], reverse=True)

        map_lookup_a = {row["map_name"]: row for row in payload_a["map_rows"]}
        map_lookup_b = {row["map_name"]: row for row in payload_b["map_rows"]}
        for map_name in sorted(set(map_lookup_a) & set(map_lookup_b)):
            shared_maps.append(
                {
                    "map_name": map_name,
                    "player_a_maps": map_lookup_a[map_name]["maps"],
                    "player_b_maps": map_lookup_b[map_name]["maps"],
                }
            )
        shared_maps.sort(key=lambda row: row["player_a_maps"] + row["player_b_maps"], reverse=True)

    return render_template(
        "player_compare.html",
        player_options=options,
        selected_player_a_id=player_a_id,
        selected_player_b_id=player_b_id,
        player_a=payload_a,
        player_b=payload_b,
        shared_heroes=shared_heroes[:10],
        shared_maps=shared_maps[:10],
    )


@app.route("/teams/<int:team_id>/players/create", methods=["POST"])
def create_player(team_id: int):
    db = get_db()
    team_exists = db.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team_exists is None:
        abort(404)

    name = request.form.get("name", "").strip()
    role = normalize_player_role(request.form.get("role", ""))
    main_hero = request.form.get("main_hero", "").strip()
    notes = request.form.get("notes", "").strip()

    if not name:
        flash("Player name is required.", "error")
        return redirect(url_for("team_detail", team_id=team_id))

    try:
        db.execute(
            """
            INSERT INTO players (team_id, name, role, main_hero, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (team_id, name, role, main_hero, notes),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash("A player with that name already exists on this team.", "error")
        return redirect(url_for("team_detail", team_id=team_id))

    flash("Player added.", "success")
    return redirect(url_for("team_detail", team_id=team_id))


@app.route("/players/<int:player_id>/edit", methods=["POST"])
def edit_player(player_id: int):
    db = get_db()
    row = db.execute("SELECT team_id FROM players WHERE id = ?", (player_id,)).fetchone()
    if row is None:
        abort(404)

    name = request.form.get("name", "").strip()
    role = normalize_player_role(request.form.get("role", ""))
    main_hero = request.form.get("main_hero", "").strip()
    notes = request.form.get("notes", "").strip()

    if not name:
        flash("Player name is required.", "error")
        return redirect(url_for("team_detail", team_id=row["team_id"]))

    try:
        db.execute(
            """
            UPDATE players
            SET name = ?, role = ?, main_hero = ?, notes = ?
            WHERE id = ?
            """,
            (name, role, main_hero, notes, player_id),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash("A player with that name already exists on this team.", "error")
        return redirect(url_for("team_detail", team_id=row["team_id"]))

    flash("Player updated.", "success")
    return redirect(url_for("team_detail", team_id=row["team_id"]))


@app.route("/players/<int:player_id>/delete", methods=["POST"])
def delete_player(player_id: int):
    db = get_db()
    row = db.execute("SELECT team_id FROM players WHERE id = ?", (player_id,)).fetchone()
    if row is None:
        abort(404)

    db.execute("DELETE FROM players WHERE id = ?", (player_id,))
    db.commit()
    flash("Player removed.", "success")
    return redirect(url_for("team_detail", team_id=row["team_id"]))


@app.route("/teams/<int:team_id>/delete", methods=["POST"])
def delete_team(team_id: int):
    db = get_db()
    deleted = db.execute("DELETE FROM teams WHERE id = ?", (team_id,)).rowcount
    db.commit()
    if not deleted:
        abort(404)
    flash("Team deleted.", "success")
    return redirect(url_for("teams"))


# Enemy Team Routes
@app.route("/teams/<int:team_id>/enemies/create", methods=["POST"])
def create_enemy_team(team_id: int):
    db = get_db()
    team = db.execute("SELECT id FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    logo_path = save_team_logo(request.files.get("logo"), name)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if not name:
        msg = "Enemy team name is required."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("team_detail", team_id=team_id))

    try:
        db.execute(
            "INSERT INTO enemy_teams (team_id, name, notes, logo_path) VALUES (?, ?, ?, ?)",
            (team_id, name, notes, logo_path),
        )
        db.commit()
        if is_ajax:
            return jsonify({"success": f"Enemy team '{name}' created."}), 200
        flash(f"Enemy team '{name}' created.", "success")
    except sqlite3.IntegrityError:
        msg = "This enemy team already exists for your team."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")

    if not is_ajax:
        return redirect(url_for("team_detail", team_id=team_id))
    return jsonify({"success": "Enemy team created."}), 200


@app.route("/enemies/<int:enemy_team_id>/edit", methods=["POST"])
def edit_enemy_team(enemy_team_id: int):
    db = get_db()
    row = db.execute("SELECT team_id, logo_path FROM enemy_teams WHERE id = ?", (enemy_team_id,)).fetchone()
    if row is None:
        abort(404)

    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    remove_logo = request.form.get("remove_logo", "").strip() == "1"
    new_logo_path = save_team_logo(request.files.get("logo"), name)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if not name:
        msg = "Enemy team name is required."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("team_detail", team_id=row["team_id"]))

    try:
        logo_path = row["logo_path"]
        if new_logo_path:
            logo_path = new_logo_path
            if row["logo_path"] and row["logo_path"] != new_logo_path:
                delete_team_logo_file(row["logo_path"])
        elif remove_logo and row["logo_path"]:
            logo_path = ""
            delete_team_logo_file(row["logo_path"])

        db.execute(
            "UPDATE enemy_teams SET name = ?, notes = ?, logo_path = ? WHERE id = ?",
            (name, notes, logo_path, enemy_team_id),
        )
        db.commit()
        if is_ajax:
            return jsonify({"success": "Enemy team updated."}), 200
        flash("Enemy team updated.", "success")
    except sqlite3.IntegrityError:
        msg = "This enemy team name already exists."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")

    if not is_ajax:
        return redirect(url_for("team_detail", team_id=row["team_id"]))
    return jsonify({"success": "Enemy team updated."}), 200


@app.route("/enemies/<int:enemy_team_id>/delete", methods=["POST"])
def delete_enemy_team(enemy_team_id: int):
    db = get_db()
    row = db.execute("SELECT team_id, logo_path FROM enemy_teams WHERE id = ?", (enemy_team_id,)).fetchone()
    if row is None:
        abort(404)

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    db.execute("DELETE FROM enemy_teams WHERE id = ?", (enemy_team_id,))
    db.commit()
    if row["logo_path"]:
        delete_team_logo_file(row["logo_path"])
    
    if is_ajax:
        return jsonify({"success": "Enemy team removed."}), 200
    flash("Enemy team removed.", "success")
    return redirect(url_for("team_detail", team_id=row["team_id"]))


@app.route("/enemies/<int:enemy_team_id>/players/create", methods=["POST"])
def create_enemy_player(enemy_team_id: int):
    db = get_db()
    row = db.execute("SELECT team_id FROM enemy_teams WHERE id = ?", (enemy_team_id,)).fetchone()
    if row is None:
        abort(404)

    name = request.form.get("name", "").strip()
    role = request.form.get("role", "").strip()
    main_hero = request.form.get("main_hero", "").strip()
    notes = request.form.get("notes", "").strip()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if not name:
        msg = "Player name is required."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("team_detail", team_id=row["team_id"]))

    try:
        db.execute(
            """
            INSERT INTO enemy_players (enemy_team_id, name, role, main_hero, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (enemy_team_id, name, role, main_hero, notes),
        )
        db.commit()
        if is_ajax:
            return jsonify({"success": f"Player '{name}' added to enemy team."}), 200
        flash(f"Player '{name}' added to enemy team.", "success")
    except sqlite3.IntegrityError:
        msg = "This player already exists on this enemy team."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")

    if not is_ajax:
        return redirect(url_for("team_detail", team_id=row["team_id"]))
    return jsonify({"success": "Player added."}), 200


@app.route("/enemy-players/<int:enemy_player_id>/edit", methods=["POST"])
def edit_enemy_player(enemy_player_id: int):
    db = get_db()
    row = db.execute(
        "SELECT enemy_team_id FROM enemy_players WHERE id = ?", (enemy_player_id,)
    ).fetchone()
    if row is None:
        abort(404)

    enemy_team_row = db.execute(
        "SELECT team_id FROM enemy_teams WHERE id = ?", (row["enemy_team_id"],)
    ).fetchone()
    if enemy_team_row is None:
        abort(404)

    name = request.form.get("name", "").strip()
    role = request.form.get("role", "").strip()
    main_hero = request.form.get("main_hero", "").strip()
    notes = request.form.get("notes", "").strip()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if not name:
        msg = "Player name is required."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("team_detail", team_id=enemy_team_row["team_id"]))

    try:
        db.execute(
            """
            UPDATE enemy_players
            SET name = ?, role = ?, main_hero = ?, notes = ?
            WHERE id = ?
            """,
            (name, role, main_hero, notes, enemy_player_id),
        )
        db.commit()
        if is_ajax:
            return jsonify({"success": "Enemy player updated."}), 200
        flash("Enemy player updated.", "success")
    except sqlite3.IntegrityError:
        msg = "A player with that name already exists on this enemy team."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")

    if not is_ajax:
        return redirect(url_for("team_detail", team_id=enemy_team_row["team_id"]))
    return jsonify({"success": "Enemy player updated."}), 200


@app.route("/enemy-players/<int:enemy_player_id>/delete", methods=["POST"])
def delete_enemy_player(enemy_player_id: int):
    db = get_db()
    row = db.execute(
        "SELECT enemy_team_id FROM enemy_players WHERE id = ?", (enemy_player_id,)
    ).fetchone()
    if row is None:
        abort(404)

    enemy_team_row = db.execute(
        "SELECT team_id FROM enemy_teams WHERE id = ?", (row["enemy_team_id"],)
    ).fetchone()
    if enemy_team_row is None:
        abort(404)

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    db.execute("DELETE FROM enemy_players WHERE id = ?", (enemy_player_id,))
    db.commit()
    
    if is_ajax:
        return jsonify({"success": "Enemy player removed."}), 200
    flash("Enemy player removed.", "success")
    return redirect(url_for("team_detail", team_id=enemy_team_row["team_id"]))


@app.route("/enemies/<int:enemy_team_id>")
def enemy_team_detail(enemy_team_id: int):
    db = get_db()
    enemy_team = db.execute(
        "SELECT id, team_id, name, notes, logo_path, created_at FROM enemy_teams WHERE id = ?",
        (enemy_team_id,),
    ).fetchone()
    if enemy_team is None:
        abort(404)

    main_team = db.execute(
        "SELECT * FROM teams WHERE id = ?", (enemy_team["team_id"],)
    ).fetchone()
    if main_team is None:
        abort(404)

    enemy_players = db.execute(
        "SELECT id, name, role, main_hero, notes FROM enemy_players WHERE enemy_team_id = ? ORDER BY name COLLATE NOCASE",
        (enemy_team_id,),
    ).fetchall()

    all_matched_enemy_scrims = []
    enemy_name_lower = (enemy_team["name"] or "").strip().lower()
    for scrim in SCRIMS:
        if scrim.get("team_id") != main_team["id"]:
            continue

        scrim_enemy_id = scrim.get("enemy_team_id")
        if scrim_enemy_id == enemy_team_id:
            all_matched_enemy_scrims.append(scrim)
            continue

        scrim_enemy_name = (scrim.get("enemy_team") or scrim.get("opponent") or "").strip().lower()
        if scrim_enemy_name and scrim_enemy_name == enemy_name_lower:
            all_matched_enemy_scrims.append(scrim)

    season_options = get_scrim_season_options(all_matched_enemy_scrims)
    default_season = get_current_season_from_recent_scrim(all_matched_enemy_scrims)
    has_unseasoned_scrims = any(
        not normalize_season_value(scrim.get("season", "")) for scrim in all_matched_enemy_scrims
    )
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )
    matched_enemy_scrims = filter_scrims_by_season(all_matched_enemy_scrims, selected_season)

    enemy_perspective_scrims = to_enemy_perspective_scrims(matched_enemy_scrims)
    predictor_inputs = {
        field_key: (request.args.get(field_key) or "").strip()
        for field_key in PREDICTOR_INPUT_ORDER
    }
    draft_predictor = build_draft_predictor(enemy_perspective_scrims, predictor_inputs)

    enemy_analytics = build_scrim_analytics(enemy_perspective_scrims)
    enemy_map_cards, enemy_map_mode_rows, enemy_best_mode, enemy_worst_mode = build_map_mode_breakdown(
        enemy_perspective_scrims
    )

    timeline_seed = sorted(
        enemy_perspective_scrims,
        key=lambda s: ((s.get("scrim_date") or "").strip(), s.get("id", 0)),
    )
    enemy_timeline_points = []
    running_maps = 0
    running_wins = 0
    for scrim in timeline_seed:
        map_entries = scrim.get("maps", [])
        maps_played = len(map_entries)
        wins = sum(1 for m in map_entries if m.get("result") == "Win")
        losses = sum(1 for m in map_entries if m.get("result") == "Loss")
        scrim_rate = round((wins / maps_played) * 100, 1) if maps_played else 0.0

        running_maps += maps_played
        running_wins += wins
        cumulative_rate = round((running_wins / running_maps) * 100, 1) if running_maps else 0.0

        scrim_date = (scrim.get("scrim_date") or "").strip()
        enemy_timeline_points.append(
            {
                "label": scrim_date if scrim_date else f"Scrim #{scrim.get('id', '?')}",
                "maps": maps_played,
                "wins": wins,
                "losses": losses,
                "scrim_win_rate": scrim_rate,
                "cumulative_win_rate": cumulative_rate,
            }
        )

    players = []
    for player in enemy_players:
        players.append({
            "id": player["id"],
            "name": player["name"],
            "role": player["role"],
            "main_hero": player["main_hero"],
            "notes": player["notes"],
        })

    return render_template(
        "enemy_team_detail.html",
        main_team=main_team,
        enemy_team=enemy_team,
        players=players,
        enemy_analytics=enemy_analytics,
        season_options=season_options,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
        has_unseasoned_scrims=has_unseasoned_scrims,
        selected_season=selected_season,
        enemy_scrim_count=len(matched_enemy_scrims),
        enemy_scrim_total_count=len(all_matched_enemy_scrims),
        enemy_map_cards=enemy_map_cards,
        enemy_map_mode_rows=enemy_map_mode_rows,
        enemy_best_mode=enemy_best_mode,
        enemy_worst_mode=enemy_worst_mode,
        enemy_timeline_points=enemy_timeline_points,
        draft_predictor=draft_predictor,
        player_roles=PLAYER_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
    )


@app.route("/enemies/<int:enemy_team_id>/draft-predict")
def enemy_draft_predict(enemy_team_id: int):
    db = get_db()
    enemy_team = db.execute(
        "SELECT id, team_id, name FROM enemy_teams WHERE id = ?",
        (enemy_team_id,),
    ).fetchone()
    if enemy_team is None:
        abort(404)

    main_team = db.execute(
        "SELECT * FROM teams WHERE id = ?",
        (enemy_team["team_id"],),
    ).fetchone()
    if main_team is None:
        abort(404)

    all_matched_enemy_scrims = []
    enemy_name_lower = (enemy_team["name"] or "").strip().lower()
    for scrim in SCRIMS:
        if scrim.get("team_id") != main_team["id"]:
            continue

        scrim_enemy_id = scrim.get("enemy_team_id")
        if scrim_enemy_id == enemy_team_id:
            all_matched_enemy_scrims.append(scrim)
            continue

        scrim_enemy_name = (scrim.get("enemy_team") or scrim.get("opponent") or "").strip().lower()
        if scrim_enemy_name and scrim_enemy_name == enemy_name_lower:
            all_matched_enemy_scrims.append(scrim)

    season_options = get_scrim_season_options(all_matched_enemy_scrims)
    default_season = get_current_season_from_recent_scrim(all_matched_enemy_scrims)
    has_unseasoned_scrims = any(
        not normalize_season_value(scrim.get("season", "")) for scrim in all_matched_enemy_scrims
    )
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )

    matched_enemy_scrims = filter_scrims_by_season(all_matched_enemy_scrims, selected_season)
    enemy_perspective_scrims = to_enemy_perspective_scrims(matched_enemy_scrims)
    predictor_inputs = {
        field_key: (request.args.get(field_key) or "").strip()
        for field_key in PREDICTOR_INPUT_ORDER
    }
    return jsonify(build_draft_predictor(enemy_perspective_scrims, predictor_inputs))


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
            filtered = [s for s in filtered if s.get("team_id") == tid]
        except (ValueError, TypeError):
            selected_team_id = ""

    return render_template(
        "scrims.html",
        scrims=list(reversed(filtered)),
        teams=teams,
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

# Build a lookup: lowercase submap name → parent map name (e.g. "frozen airfield" → "Hell's Haven")
_SUBMAP_PARENT: dict[str, str] = {}
for _parent, _subs in MAP_SUBMAPS.items():
    for _s in _subs:
        _SUBMAP_PARENT[_s.lower()] = _parent


def _strip_bracket_hint(name: str) -> str:
    """Remove trailing parenthetical abbreviation hints like '(FA, SSF, EM)'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()


def _match_map_name(raw: str) -> str:
    """
    Try to find the closest canonical map name from MAPS for a raw string.
    Falls back to the raw string stripped of bracket hints if no match found.
    """
    base = _strip_bracket_hint(raw)
    base_lower = base.lower()
    # Exact match
    for m in MAPS:
        if m.lower() == base_lower:
            return m
    # Prefix match: raw starts with canonical map name
    for m in sorted(MAPS, key=len, reverse=True):
        if base_lower.startswith(m.lower()):
            return m
    return base


def _compact_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


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

    alias_lookup = {
        "daredevil": "Daredevil",
        "captainamerica": "Captain America",
        "captianamerica": "Captain America",
        "starlord": "Star-Lord",
        "spiderman": "Spider-Man",
        "wintersolider": "Winter Soldier",
        "cloakdagger": "Cloak & Dagger",
        "rocketraccoon": "Rocket Raccoon",
        "adamwarlock": "Adam Warlock",
    }
    mapped = alias_lookup.get(compact)
    if mapped and mapped in HERO_TRANSFORMATIONS:
        return mapped

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


def _hero_image_url(hero_name: str) -> str:
    transform_key = _resolve_hero_transform_key(hero_name)
    if transform_key:
        images = HERO_TRANSFORMATIONS.get(transform_key) or []
        if images:
            return f"https://marvelrivalsapi.com/rivals{images[0]}"
    safe_text = quote((hero_name or "Hero")[:24])
    return f"https://via.placeholder.com/80/111827/e6edf3?text={safe_text}"


@app.context_processor
def inject_template_helpers():
    return {
        "hero_image_url": _hero_image_url,
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
        return [{"hero": h.strip(), "player": ""} for h in row[sl]]

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


@app.route("/scrims/import-csv", methods=["POST"])
def import_csv_scrims():
    global NEXT_SCRIM_ID, NEXT_MAP_ID

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

    parsed_scrims, warnings = _parse_csv_into_scrims(raw_text, team_id, team_name)

    if not parsed_scrims:
        flash("No scrims could be imported from that CSV. " + " ".join(warnings), "error")
        return redirect(url_for("scrims"))

    # Try to match enemy team names to existing teams in the global team database
    db = get_db()
    migrate_enemy_teams_to_team_database(db)
    enemy_rows = db.execute(
        "SELECT id, name FROM teams WHERE id != ?", (team_id,)
    ).fetchall() if team_id else []
    enemy_lookup = {r["name"].lower(): r["id"] for r in enemy_rows}

    imported = 0
    for scrim in parsed_scrims:
        scrim["season"] = season
        normalize_scrim_record(scrim)
        # Assign IDs
        scrim["id"] = NEXT_SCRIM_ID
        NEXT_SCRIM_ID += 1

        # Try to match enemy team
        ename_lower = scrim["enemy_team"].lower()
        if ename_lower in enemy_lookup:
            scrim["enemy_team_id"] = enemy_lookup[ename_lower]

        for map_entry in scrim["maps"]:
            map_entry["id"] = NEXT_MAP_ID
            NEXT_MAP_ID += 1
            for event in map_entry.get("events", []):
                event["id"] = NEXT_EVENT_ID
                NEXT_EVENT_ID += 1

        SCRIMS.append(scrim)
        imported += 1

    save_app_state()

    msg = f"Imported {imported} scrim{'s' if imported != 1 else ''}."
    if warnings:
        msg += " Warnings: " + " | ".join(warnings)
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
                slots = _sanitize_simulator_draft_slots(json.loads(row["draft_slots_json"] or "{}"))
            except json.JSONDecodeError:
                slots = _sanitize_simulator_draft_slots({})

            drafts.append(
                {
                    "id": row["id"],
                    "name": row["draft_name"],
                    "season": row["season"],
                    "slots": slots,
                    "created_at": row["created_at"],
                }
            )
        return jsonify(drafts)

    payload = request.get_json(silent=True) or {}
    draft_name = (payload.get("name") or "").strip()
    season = normalize_season_value(payload.get("season", ""))
    slots = _sanitize_simulator_draft_slots(payload.get("slots"))

    if not draft_name:
        return jsonify({"error": "Draft name is required."}), 400
    if len(draft_name) > 80:
        return jsonify({"error": "Draft name must be 80 characters or less."}), 400
    if not any(slots.values()):
        return jsonify({"error": "Add at least one draft hero before saving."}), 400

    cursor = db.execute(
        """
        INSERT INTO team_saved_drafts (team_id, draft_name, season, draft_slots_json)
        VALUES (?, ?, ?, ?)
        """,
        (team_id, draft_name, season, json.dumps(slots)),
    )
    db.commit()

    return jsonify(
        {
            "id": cursor.lastrowid,
            "name": draft_name,
            "season": season,
            "slots": slots,
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

    team_id = parse_team_id(request.form.get("team_id", ""))
    team_name = get_team_name_by_id(team_id)
    team_slot = normalize_match_team_slot(request.form.get("team_slot", "team1"))
    if not team_name:
        flash("Please assign this scrim to your team.", "error")
        return redirect(f"{url_for('scrims')}#create-scrim")

    enemy_team = request.form.get("enemy_team_manual", "").strip() or request.form.get("opponent", "").strip()
    enemy_team_id = request.form.get("enemy_team_id", "").strip()
    scrim_date = request.form.get("scrim_date", "").strip()
    season = normalize_season_value(request.form.get("season", ""))
    notes = request.form.get("notes", "").strip()

    if not season:
        flash("Please set a season for this scrim.", "error")
        return redirect(f"{url_for('scrims')}#create-scrim")

    scrim = {
        "id": NEXT_SCRIM_ID,
        "opponent": enemy_team,
        "enemy_team": enemy_team,
        "enemy_team_id": int(enemy_team_id) if enemy_team_id else None,
        "scrim_date": scrim_date,
        "season": season,
        "team_id": team_id,
        "team_name": team_name,
        "team_slot": team_slot,
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

    wins = sum(1 for m in scrim["maps"] if m["result"] == "Win")
    losses = sum(1 for m in scrim["maps"] if m["result"] == "Loss")

    return render_template(
        "scrim_detail.html",
        scrim=scrim,
        maps=MAPS,
        map_images=MAP_IMAGES,
        map_submaps=MAP_SUBMAPS,
        sides=SIDES,
        results=RESULTS,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        teams=teams,
        wins=wins,
        losses=losses,
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
        opponent_field_label="Enemy Team",
        show_team_selector=True,
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
    team1_map_wins = sum(
        1
        for map_entry in tournament_match.get("maps", [])
        if get_result_for_slot(
            map_entry,
            get_tournament_team_slot_for_map(map_entry, tournament_match.get("team1_tournament_team_id")) or "team1",
        ) == "Win"
    )
    team2_map_wins = sum(
        1
        for map_entry in tournament_match.get("maps", [])
        if get_result_for_slot(
            map_entry,
            get_tournament_team_slot_for_map(map_entry, tournament_match.get("team2_tournament_team_id")) or "team2",
        ) == "Win"
    )

    return render_template(
        "tournament_match_detail.html",
        tournament=tournament_record,
        match=tournament_match,
        team1_map_wins=team1_map_wins,
        team2_map_wins=team2_map_wins,
        maps=MAPS,
        map_images=MAP_IMAGES,
        map_submaps=MAP_SUBMAPS,
        sides=SIDES,
        results=RESULTS,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
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
    team_id = parse_team_id(request.form.get("team_id", ""))
    team_name = get_team_name_by_id(team_id)
    team_slot = normalize_match_team_slot(request.form.get("team_slot", scrim.get("team_slot", "team1")))
    if not team_name:
        flash("Please assign this scrim to your team.", "error")
        return redirect(url_for("scrim_detail", scrim_id=scrim_id))

    enemy_team = request.form.get("enemy_team", scrim.get("enemy_team", scrim.get("opponent", ""))).strip()
    season = normalize_season_value(request.form.get("season", scrim.get("season", "")))
    if not season:
        flash("Please set a season for this scrim.", "error")
        return redirect(url_for("scrim_detail", scrim_id=scrim_id))

    scrim["opponent"] = enemy_team
    scrim["enemy_team"] = enemy_team
    scrim["scrim_date"] = request.form.get("scrim_date", scrim["scrim_date"]).strip()
    scrim["season"] = season
    scrim["team_id"] = team_id
    scrim["team_name"] = team_name
    scrim["team_slot"] = team_slot
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

    tournament_match["team1_name"] = team1_name
    tournament_match["team2_name"] = team2_name
    tournament_match["team1_enemy_id"] = None
    tournament_match["team2_enemy_id"] = None
    tournament_match["team1_players"] = parse_name_list(request.form.get("team1_players", ""))
    tournament_match["team2_players"] = parse_name_list(request.form.get("team2_players", ""))
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

    scrim["maps"].append(build_match_map_entry_from_form())
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
    map_entry["result"] = request.form.get("result", map_entry["result"]).strip()
    if map_entry["result"] not in RESULTS:
        map_entry["result"] = ""
    map_entry["score"] = request.form.get("score", map_entry.get("score", "")).strip()
    our_team_slot = request.form.get("our_team_slot", map_entry.get("our_team_slot", "team1")).strip()
    map_entry["our_team_slot"] = our_team_slot if our_team_slot in TEAM_SLOTS else "team1"

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

    for i in range(6):
        section["team1"][i]["hero"] = request.form.get(f"team1_hero_{i}", "").strip()
        section["team1"][i]["player"] = request.form.get(f"team1_player_{i}", "").strip()
        section["team2"][i]["hero"] = request.form.get(f"team2_hero_{i}", "").strip()
        section["team2"][i]["player"] = request.form.get(f"team2_player_{i}", "").strip()
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
    map_entry["result"] = request.form.get("result", map_entry["result"]).strip()
    if map_entry["result"] not in RESULTS:
        map_entry["result"] = ""
    map_entry["score"] = request.form.get("score", map_entry.get("score", "")).strip()
    our_team_slot = request.form.get("our_team_slot", map_entry.get("our_team_slot", "team1")).strip()
    map_entry["our_team_slot"] = our_team_slot if our_team_slot in TEAM_SLOTS else "team1"
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

    use_section_sides = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), [])) or map_entry.get("map_name") in ATTACK_DEFENSE_MAPS
    section = sections[section_index]

    section["submap"] = request.form.get("submap", section.get("submap", "")).strip()
    side_value = request.form.get("side", section.get("side", "")).strip() if use_section_sides else ""
    if side_value not in SIDES:
        side_value = ""
    section["side"] = side_value
    section["score"] = build_score_text(
        request.form.get("score_team1", "").strip(),
        request.form.get("score_team2", "").strip(),
        request.form.get("score", section.get("score", "")).strip(),
    )

    for team in ("team1", "team2"):
        team_slots = []
        for i in range(6):
            hero = request.form.get(f"{team}_hero_{i}", "").strip()
            player = request.form.get(f"{team}_player_{i}", "").strip()
            team_slots.append({"hero": hero, "player": player})
        section[team] = team_slots

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

    use_section_sides = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), [])) or map_entry.get("map_name") in ATTACK_DEFENSE_MAPS
    section = sections[section_index]
    section["submap"] = request.form.get("submap", section.get("submap", "")).strip()
    side_value = request.form.get("side", section.get("side", "")).strip() if use_section_sides else ""
    if side_value not in SIDES:
        side_value = ""
    section["side"] = side_value
    section["score"] = build_score_text(
        request.form.get("score_team1", "").strip(),
        request.form.get("score_team2", "").strip(),
        request.form.get("score", section.get("score", "")).strip(),
    )

    for team in ("team1", "team2"):
        team_slots = []
        for i in range(6):
            hero = request.form.get(f"{team}_hero_{i}", "").strip()
            player = request.form.get(f"{team}_player_{i}", "").strip()
            team_slots.append({"hero": hero, "player": player})
        section[team] = team_slots

    map_entry["comp"][section_index] = section
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


init_db()
load_app_state()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
    )