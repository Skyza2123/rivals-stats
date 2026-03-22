import csv
import io
import os
import json
import re
import sqlite3
import importlib
from difflib import SequenceMatcher
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
from flask import Flask, render_template, request, redirect, url_for, abort, g, flash, jsonify
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
PREDICTOR_GROUPS = (
    (("team1", "ban1", "t1_ban1"), ("team2", "ban1", "t2_ban1")),
    (("team2", "protect1", "t2_protect1"),),
    (("team1", "ban2", "t1_ban2"), ("team1", "protect1", "t1_protect1")),
    (("team1", "ban3", "t1_ban3"), ("team2", "ban2", "t2_ban2")),
    (("team1", "protect2", "t1_protect2"),),
    (("team2", "ban3", "t2_ban3"), ("team2", "ban4", "t2_ban4"), ("team2", "protect2", "t2_protect2")),
    (("team1", "ban4", "t1_ban4"),),
)
ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

SCRIMS = []
NEXT_SCRIM_ID = 1
NEXT_MAP_ID = 1
NEXT_EVENT_ID = 1


def ensure_state_defaults() -> None:
    global SCRIMS, NEXT_SCRIM_ID, NEXT_MAP_ID, NEXT_EVENT_ID
    if not isinstance(SCRIMS, list):
        SCRIMS = []
    for scrim in SCRIMS:
        if isinstance(scrim, dict):
            normalize_scrim_record(scrim)
    NEXT_SCRIM_ID = max(1, int(NEXT_SCRIM_ID or 1))
    NEXT_MAP_ID = max(1, int(NEXT_MAP_ID or 1))
    NEXT_EVENT_ID = max(1, int(NEXT_EVENT_ID or 1))


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
            """
        )
        for key, default in (
            ("scrims", "[]"),
            ("next_scrim_id", "1"),
            ("next_map_id", "1"),
            ("next_event_id", "1"),
        ):
            conn.execute(
                "INSERT OR IGNORE INTO app_state (state_key, state_value) VALUES (?, ?)",
                (key, default),
            )

        team_columns = {row[1] for row in conn.execute("PRAGMA table_info(teams)").fetchall()}
        if "logo_path" not in team_columns:
            conn.execute("ALTER TABLE teams ADD COLUMN logo_path TEXT NOT NULL DEFAULT ''")

        enemy_team_columns = {row[1] for row in conn.execute("PRAGMA table_info(enemy_teams)").fetchall()}
        if "logo_path" not in enemy_team_columns:
            conn.execute("ALTER TABLE enemy_teams ADD COLUMN logo_path TEXT NOT NULL DEFAULT ''")

        TEAM_LOGO_DIR.mkdir(parents=True, exist_ok=True)
        conn.commit()
    finally:
        conn.close()


def load_app_state() -> None:
    global SCRIMS, NEXT_SCRIM_ID, NEXT_MAP_ID, NEXT_EVENT_ID
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT state_key, state_value FROM app_state").fetchall()
        state = {row["state_key"]: row["state_value"] for row in rows}

        SCRIMS = json.loads(state.get("scrims", "[]"))
        NEXT_SCRIM_ID = int(state.get("next_scrim_id", "1"))
        NEXT_MAP_ID = int(state.get("next_map_id", "1"))
        NEXT_EVENT_ID = int(state.get("next_event_id", "1"))
        ensure_state_defaults()
    finally:
        conn.close()


@app.before_request
def refresh_app_state_from_db() -> None:
    # Keep in-memory state in sync across hosted worker processes.
    load_app_state()


def save_app_state() -> None:
    ensure_state_defaults()
    db = get_db()
    db.executemany(
        "UPDATE app_state SET state_value = ? WHERE state_key = ?",
        [
            (json.dumps(SCRIMS), "scrims"),
            (str(NEXT_SCRIM_ID), "next_scrim_id"),
            (str(NEXT_MAP_ID), "next_map_id"),
            (str(NEXT_EVENT_ID), "next_event_id"),
        ],
    )
    db.commit()


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


def normalize_scrim_record(scrim: dict) -> dict:
    scrim["season"] = normalize_season_value(scrim.get("season", ""))
    return scrim


def get_scrim_season_options(scrims: list[dict]) -> list[str]:
    seasons = {
        normalize_season_value(scrim.get("season", ""))
        for scrim in scrims
        if normalize_season_value(scrim.get("season", ""))
    }
    return sorted(seasons, key=lambda value: [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)])


def get_selected_season(raw_value: str, season_options: list[str]) -> str:
    selected = normalize_season_value(raw_value)
    if not selected or selected.lower() == "all":
        return "all"
    return selected if selected in season_options else "all"


def filter_scrims_by_season(scrims: list[dict], season: str) -> list[dict]:
    selected = normalize_season_value(season)
    if not selected or selected.lower() == "all":
        return scrims
    return [scrim for scrim in scrims if normalize_season_value(scrim.get("season", "")) == selected]


def compute_player_stats(player_name: str) -> dict:
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

    for scrim in SCRIMS:
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


def get_scrim_or_404(scrim_id: int) -> dict:
    for scrim in SCRIMS:
        if scrim["id"] == scrim_id:
            return scrim
    abort(404)


def get_map_or_404(scrim: dict, map_id: int) -> dict:
    for map_entry in scrim["maps"]:
        if map_entry["id"] == map_id:
            return map_entry
    abort(404)


def parse_comma_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


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


def build_default_comp_sections(map_name: str) -> list[dict]:
    submaps = MAP_SUBMAPS.get(map_name, [])
    if submaps:
        return [
            {
                "submap": sm,
                "side": "",
                "score": "",
                "team1": [{"hero": "", "player": ""} for _ in range(6)],
                "team2": [{"hero": "", "player": ""} for _ in range(6)],
            }
            for sm in submaps
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


def build_scrim_analytics(scrims: list[dict]) -> dict:
    ban_slot_keys = ("ban1", "ban2", "ban3", "ban4")
    ban_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    enemy_ban_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    ban_position_stats = {slot: defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0}) for slot in ban_slot_keys}
    enemy_ban_position_stats = {slot: defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0}) for slot in ban_slot_keys}
    protect_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    hero_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})
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

            for hero_name in heroes_in_map:
                hero_stats[hero_name]["maps"] += 1
                if is_win:
                    hero_stats[hero_name]["wins"] += 1
                elif is_loss:
                    hero_stats[hero_name]["losses"] += 1

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
                "win_rate": pct(stats["wins"], stats["count"]),
            }
        )
    ban_rows.sort(key=lambda r: (r["count"], r["win_rate"]), reverse=True)

    enemy_ban_rows = []
    for hero, stats in enemy_ban_stats.items():
        enemy_ban_rows.append(
            {
                "hero": hero,
                "count": stats["count"],
                "ban_rate": pct(stats["count"], total_enemy_filled_bans),
                "win_rate": pct(stats["wins"], stats["count"]),
            }
        )
    enemy_ban_rows.sort(key=lambda r: (r["count"], r["win_rate"]), reverse=True)

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

    main_ban_variation = build_ban_phase_variation_summary(ban_position_rows, "Main team")
    enemy_ban_variation = build_ban_phase_variation_summary(enemy_ban_position_rows, "Enemy team")

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
        "ban_diff_rows": ban_diff_rows[:12],
        "ban_next_rows": ban_next_rows[:12],
        "ban_to_protect_rows": ban_to_protect_rows[:12],
        "draft_route_rows": draft_route_rows[:16],
        "second_order_ban_rows": second_order_ban_rows[:12],
        "protect1_influence_rows": protect1_influence_rows[:12],
        "lead_to_ban_rows": lead_to_ban_rows[:12],
        "lead_to_protect_rows": lead_to_protect_rows[:12],
        "ban_protect_rows": ban_protect_rows[:12],
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
                numeric_score = _parse_score_number(section.get("score", ""))
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
    total_maps = sum(len(scrim["maps"]) for scrim in SCRIMS)
    total_events = sum(len(map_entry["events"]) for scrim in SCRIMS for map_entry in scrim["maps"])
    total_teams = db.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    total_players = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]

    return render_template(
        "dashboard.html",
        total_scrims=total_scrims,
        total_maps=total_maps,
        total_events=total_events,
        total_teams=total_teams,
        total_players=total_players,
        recent_scrims=list(reversed(SCRIMS[-5:])),
    )


@app.route("/teams")
def teams():
    db = get_db()
    team_rows = db.execute(
        """
        SELECT t.id, t.name, t.notes, t.logo_path, COUNT(p.id) AS player_count
        FROM teams t
        LEFT JOIN players p ON p.team_id = t.id
        GROUP BY t.id
        ORDER BY t.name COLLATE NOCASE
        """
    ).fetchall()
    teams_with_scrim_stats = []
    for row in team_rows:
        team_scrims = [
            scrim
            for scrim in SCRIMS
            if scrim.get("team_id") == row["id"]
            or (
                not scrim.get("team_id")
                and (scrim.get("team_name", "") or "").strip().lower() == row["name"].strip().lower()
            )
        ]
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
                "player_count": row["player_count"],
                "scrim_count": len(team_scrims),
                "map_count": team_maps,
                "map_win_rate": team_win_rate,
            }
        )

    return render_template("teams.html", teams=teams_with_scrim_stats)


@app.route("/teams/create", methods=["POST"])
def create_team():
    db = get_db()
    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    logo_path = save_team_logo(request.files.get("logo"), name)

    if not name:
        flash("Team name is required.", "error")
        return redirect(url_for("teams"))

    try:
        db.execute("INSERT INTO teams (name, notes, logo_path) VALUES (?, ?, ?)", (name, notes, logo_path))
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
        db.execute("UPDATE teams SET name = ?, notes = ?, logo_path = ? WHERE id = ?", (name, notes, logo_path, team_id))
        db.commit()
    except sqlite3.IntegrityError:
        flash("A team with that name already exists.", "error")
        return redirect(url_for("team_detail", team_id=team_id))

    flash("Team updated.", "success")
    return redirect(url_for("team_detail", team_id=team_id))


@app.route("/teams/<int:team_id>")
def team_detail(team_id: int):
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
    selected_season = get_selected_season(request.args.get("season", "all"), season_options)
    team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
    predictor_inputs = {
        field_key: (request.args.get(field_key) or "").strip()
        for field_key in PREDICTOR_INPUT_ORDER
    }
    draft_predictor = build_draft_predictor(team_scrims, predictor_inputs)

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
        stats = compute_player_stats(row["name"])
        players.append({
            "id": row["id"],
            "name": row["name"],
            "role": row["role"],
            "main_hero": row["main_hero"],
            "notes": row["notes"],
            "stats": stats,
        })

    # Get enemy teams for this team
    enemy_team_rows = db.execute(
        "SELECT id, name, notes, logo_path, created_at FROM enemy_teams WHERE team_id = ? ORDER BY name COLLATE NOCASE",
        (team_id,),
    ).fetchall()

    enemy_teams = []
    for enemy_row in enemy_team_rows:
        enemy_players = db.execute(
            "SELECT id, name, role, main_hero, notes FROM enemy_players WHERE enemy_team_id = ? ORDER BY name COLLATE NOCASE",
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

    return render_template(
        "team_detail.html",
        team=team,
        players=players,
        enemy_teams=enemy_teams,
        player_roles=PLAYER_ROLES,
        team_analytics=team_analytics,
        season_options=season_options,
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
    selected_season = get_selected_season(request.args.get("season", "all"), season_options)
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
    selected_season = get_selected_season(request.args.get("season", "all"), season_options)
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
    selected_season = get_selected_season(request.args.get("season", "all"), season_options)

    matched_enemy_scrims = filter_scrims_by_season(all_matched_enemy_scrims, selected_season)
    enemy_perspective_scrims = to_enemy_perspective_scrims(matched_enemy_scrims)
    predictor_inputs = {
        field_key: (request.args.get(field_key) or "").strip()
        for field_key in PREDICTOR_INPUT_ORDER
    }
    return jsonify(build_draft_predictor(enemy_perspective_scrims, predictor_inputs))


@app.route("/scrims")
def scrims():
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    return render_template("scrims.html", scrims=list(reversed(SCRIMS)), teams=teams)


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


def _parse_score_number(raw_score: str) -> float | None:
    """Extract the first numeric value from a score string like '2-1' or '0.92'."""
    value = (raw_score or "").strip()
    if not value:
        return None
    match = re.search(r"\d+(?:\.\d+)?", value)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


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
    if not team_name:
        flash("Please select your team before importing.", "error")
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

    # Try to match enemy team names to existing enemy_team records
    db = get_db()
    enemy_rows = db.execute(
        "SELECT id, name FROM enemy_teams WHERE team_id = ?", (team_id,)
    ).fetchall() if team_id else []
    enemy_lookup = {r["name"].lower(): r["id"] for r in enemy_rows}

    imported = 0
    for scrim in parsed_scrims:
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
    """API endpoint to get enemy teams for a specific team"""
    db = get_db()
    team = db.execute("SELECT id FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    enemy_team_rows = db.execute(
        "SELECT id, name FROM enemy_teams WHERE team_id = ? ORDER BY name COLLATE NOCASE",
        (team_id,),
    ).fetchall()

    return jsonify([{
        "id": row["id"],
        "name": row["name"],
    } for row in enemy_team_rows])


@app.route("/scrims/new")
def new_scrim():
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    return render_template("new_scrim.html", today=date.today().isoformat(), teams=teams)


@app.route("/scrims/create", methods=["POST"])
def create_scrim():
    global NEXT_SCRIM_ID

    team_id = parse_team_id(request.form.get("team_id", ""))
    team_name = get_team_name_by_id(team_id)
    if not team_name:
        flash("Please assign this scrim to your team.", "error")
        return redirect(url_for("new_scrim"))

    enemy_team = request.form.get("enemy_team_manual", "").strip() or request.form.get("opponent", "").strip()
    enemy_team_id = request.form.get("enemy_team_id", "").strip()
    scrim_date = request.form.get("scrim_date", "").strip()
    season = normalize_season_value(request.form.get("season", ""))
    notes = request.form.get("notes", "").strip()

    if not season:
        flash("Please set a season for this scrim.", "error")
        return redirect(url_for("new_scrim"))

    scrim = {
        "id": NEXT_SCRIM_ID,
        "opponent": enemy_team,
        "enemy_team": enemy_team,
        "enemy_team_id": int(enemy_team_id) if enemy_team_id else None,
        "scrim_date": scrim_date,
        "season": season,
        "team_id": team_id,
        "team_name": team_name,
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
    )


@app.route("/scrims/<int:scrim_id>/edit", methods=["POST"])
def edit_scrim(scrim_id: int):
    scrim = get_scrim_or_404(scrim_id)
    team_id = parse_team_id(request.form.get("team_id", ""))
    team_name = get_team_name_by_id(team_id)
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
    scrim["notes"] = request.form.get("notes", scrim["notes"]).strip()
    save_app_state()
    return redirect(url_for("scrim_detail", scrim_id=scrim_id))


@app.route("/scrims/<int:scrim_id>/delete", methods=["POST"])
def delete_scrim(scrim_id: int):
    scrim = get_scrim_or_404(scrim_id)
    SCRIMS.remove(scrim)
    save_app_state()
    return redirect(url_for("scrims"))


@app.route("/scrims/<int:scrim_id>/add-map", methods=["POST"])
def add_map(scrim_id: int):
    global NEXT_MAP_ID

    scrim = get_scrim_or_404(scrim_id)

    map_name = request.form.get("map_name", "").strip()
    side = ""
    result = request.form.get("result", "").strip()
    our_team_slot = request.form.get("our_team_slot", "team1").strip()
    if our_team_slot not in TEAM_SLOTS:
        our_team_slot = "team1"
    score = request.form.get("score", "").strip()
    notes = request.form.get("notes", "").strip()

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

    comp_sections = build_default_comp_sections(map_name)

    map_entry = {
        "id": NEXT_MAP_ID,
        "map_name": map_name,
        "side": side,
        "our_team_slot": our_team_slot,
        "result": result,
        "score": score,
        "draft": draft,
        "comp": comp_sections,
        "notes": notes,
        "vod_url": "",
        "events": [],
    }

    scrim["maps"].append(map_entry)
    NEXT_MAP_ID += 1
    save_app_state()

    return redirect(url_for("scrim_detail", scrim_id=scrim_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>")
def map_detail(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)

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


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-notes", methods=["POST"])
def update_notes(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    map_entry["notes"] = request.form.get("notes", "").strip()
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-vod", methods=["POST"])
def update_vod(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    map_entry["vod_url"] = request.form.get("vod_url", "").strip()
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-info", methods=["POST"])
def update_map_info(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    map_entry["result"] = request.form.get("result", map_entry["result"]).strip()
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
            "score": request.form.get(f"sec_{s}_score", "").strip(),
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
    section["score"] = request.form.get("score", section.get("score", "")).strip()

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


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/delete-event/<int:event_id>", methods=["POST"])
def delete_event(scrim_id: int, map_id: int, event_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    map_entry["events"] = [e for e in map_entry["events"] if e["id"] != event_id]
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


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


@app.route("/draft-simulator")
def draft_simulator():
    return render_template(
        "draft_simulator.html",
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
    )


init_db()
load_app_state()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
    )