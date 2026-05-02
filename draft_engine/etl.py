"""
Stage 2 — ETL: transform the app_state JSON scrims into draft engine tables.

Usage
-----
    python -m draft_engine.etl                    # reads DB_PATH from env or default
    python -m draft_engine.etl --reset            # drops + recreates tables first
    python -m draft_engine.etl --db path/to/db    # explicit path

The ETL is idempotent when run without --reset: it inserts only rows that do
not already exist (INSERT OR IGNORE / INSERT OR REPLACE with UNIQUE keys).

Source data notes
-----------------
* Scrims are stored in `app_state` table, key "scrims", as a JSON array.
* Each scrim record:
    {
      id, team_name, opponent / enemy_team, enemy_team_id,
      season, scrim_date,
      maps: [
        {
          id, map_name, side, our_team_slot, result, score,
          draft: {
            team1: { ban1, ban2, ban3, ban4, protect1, protect2 },
            team2: { ban1, ban2, ban3, ban4, protect1, protect2 }
          },
          comp: [
            {
              submap, side, score,
              team1: [ {hero, player}, ... ],   # 6 slots
              team2: [ {hero, player}, ... ]
            }
          ]
        }
      ]
    }

* Tournament matches are stored separately under "tournament_matches".  The
  ETL processes both, treating tournament map records the same way.

Draft action ordering
---------------------
The game's draft phase uses a fixed order that is not stored explicitly:
  ban1 (team picking first) → ban1 (other team) → protect1 → protect1 →
  ban2 → ban2 → protect2 → protect2 → ban3 → ban3 → ban4 → ban4
  then picks are inferred as heroes in comp that weren't banned/protected.

Because the slot that bans first is ambiguous in older records (no explicit
"first pick" field), we assign order based on the key name:
  ban1_t1=1, ban1_t2=2, protect1_t1=3, protect1_t2=4,
  ban2_t1=5, ban2_t2=6, protect2_t1=7, protect2_t2=8,
  ban3_t1=9, ban3_t2=10, ban4_t1=11, ban4_t2=12
  then picks from comp in hero slot order (order 13+).

Known team-up bonuses (as of Season 7)
---------------------------------------
Hardcoded here; update as patches land.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from draft_engine.schema import create_draft_engine_tables, drop_draft_engine_tables


# ---------------------------------------------------------------------------
# Known team-up definitions
# Each entry: (teamup_name, hero_a, hero_b, hero_c_or_None)
# Heroes stored in canonical (sorted) order.
# ---------------------------------------------------------------------------
TEAM_UPS: list[tuple[str, str, str, str | None]] = [
    # 2-hero team-ups
    ("Gamma Charge",          "Hulk",            "Iron Man",         None),
    ("Fastball Special",      "Colossus",        "Wolverine",        None),
    ("Symbiote Bond",         "Spider-Man",      "Venom",            None),
    ("Thunderous Allies",     "Captain America", "Thor",             None),
    ("Guardian Revival",      "Adam Warlock",    "Star-Lord",        None),
    ("Cosmic Barrier",        "Captain Marvel",  "Magneto",          None),
    ("Soul Resonance",        "Adam Warlock",    "Mantis",           None),
    ("Ammo Overload",         "Rocket Raccoon",  "Groot",            None),
    ("Dimensional Shortcut",  "Cloak & Dagger",  "Spider-Man",       None),
    ("Chilling Presence",     "Invisible Woman", "Mr. Fantastic",    None),
    ("Gamma Power",           "Hulk",            "Bruce Banner",     None),
    ("Blind Spot",            "Black Panther",   "Storm",            None),
    ("Frozen Assets",         "Luna Snow",       "Namor",            None),
    ("Frozen Assets",         "Luna Snow",       "Jeff TLS",         None),
    ("Ragnarok Slam",         "Thor",            "Loki",             None),
    ("Eternal Bond",          "Angela",          "Thor",             None),
    ("Rock Hard",             "Thing",           "Human Torch",      None),
    ("Cybernetic Enhancement","Iron Fist",        "Punisher",         None),
    ("Hawkeye Overwatch",     "Hawkeye",         "Black Widow",      None),
    ("Voltaic Vision",        "Storm",           "Thor",             None),
    ("Galactic Infusion",     "Mantis",          "Star-Lord",        None),
    ("Shrink Ray",            "Ant-Man",         "Wasp",             None),
    ("Tactical Wit",          "Captain America", "Iron Man",         None),
    ("Crimson Alchemy",       "Scarlet Witch",   "Doctor Strange",   None),
    ("Seismic Slam",          "Groot",           "Magneto",          None),
    ("Symbiote Surge",        "Venom",           "Carnage",          None),
    # 3-hero team-ups
    ("X-Men Unity",           "Cyclops",         "Storm",            "Wolverine"),
    ("Omega-Level Threat",    "Magneto",         "Psylocke",         "Storm"),
]

# Canonical draft action ordering (key → 1-based order)
DRAFT_ACTION_ORDER: dict[tuple[str, str], int] = {
    ("team1", "ban1"): 1,
    ("team2", "ban1"): 2,
    ("team1", "protect1"): 3,
    ("team2", "protect1"): 4,
    ("team1", "ban2"): 5,
    ("team2", "ban2"): 6,
    ("team1", "protect2"): 7,
    ("team2", "protect2"): 8,
    ("team1", "ban3"): 9,
    ("team2", "ban3"): 10,
    ("team1", "ban4"): 11,
    ("team2", "ban4"): 12,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_hero(h: str) -> str:
    """Strip whitespace; return empty string for blank/None."""
    return (h or "").strip()


def _canon_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _canon_trio(a: str, b: str, c: str) -> tuple[str, str, str]:
    s = sorted([a, b, c])
    return s[0], s[1], s[2]


def _heroes_in_comp_slot(slot: list[dict]) -> list[str]:
    return [_norm_hero(e.get("hero", "")) for e in slot if _norm_hero(e.get("hero", ""))]


def _result_for_team(map_result: str, our_team_slot: str, team_slot: str) -> str:
    """
    Return 'Win' or 'Loss' for *team_slot* given the map result (which is
    always from *our_team_slot*'s perspective).
    """
    if not map_result:
        return ""
    if team_slot == our_team_slot:
        return map_result
    return "Win" if map_result == "Loss" else "Loss"


# ---------------------------------------------------------------------------
# Per-record extractors
# ---------------------------------------------------------------------------

def _extract_draft_actions(
    de_map_id: int,
    draft: dict[str, dict],
    scrim_id: int,
    map_name: str,
    season: str,
    enemy_team: str,
    comp_heroes_by_slot: dict[str, list[str]],
) -> list[tuple]:
    """
    Return rows for de_draft_actions.
    Tuple: (de_map_id, team_slot, action_type, action_order, hero,
            scrim_id, map_name, season, enemy_team_name)
    """
    rows = []

    # Bans and protects from draft dict
    for team_slot, actions in draft.items():
        if not isinstance(actions, dict):
            continue
        for key, hero in actions.items():
            hero = _norm_hero(hero)
            if not hero:
                continue
            if key.startswith("ban"):
                atype = "ban"
            elif key.startswith("protect"):
                atype = "protect"
            else:
                continue
            order = DRAFT_ACTION_ORDER.get((team_slot, key), 99)
            rows.append((de_map_id, team_slot, atype, order, hero,
                         scrim_id, map_name, season, enemy_team))

    # Picks inferred from comp (heroes not already captured as bans/protects)
    banned_or_protected: dict[str, set[str]] = {"team1": set(), "team2": set()}
    for r in rows:
        _, team_slot, _, _, hero, *_ = r
        banned_or_protected[team_slot].add(hero)

    pick_order_base = 13
    for team_slot, heroes in comp_heroes_by_slot.items():
        order = pick_order_base
        for hero in heroes:
            if hero not in banned_or_protected.get(team_slot, set()):
                rows.append((de_map_id, team_slot, "pick", order, hero,
                             scrim_id, map_name, season, enemy_team))
                banned_or_protected[team_slot].add(hero)
                order += 1

    return rows


def _upsert_pair(
    cursor: sqlite3.Cursor,
    table: str,
    team_col: str,
    team_val: str,
    hero_a: str,
    hero_b: str,
    win: bool,
    loss: bool,
    season: str,
) -> None:
    ha, hb = _canon_pair(hero_a, hero_b)
    cursor.execute(
        f"""
        INSERT INTO {table} ({team_col}, hero_a, hero_b, co_appearances, wins, losses, season)
        VALUES (?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT({team_col}, hero_a, hero_b, season) DO UPDATE SET
            co_appearances = co_appearances + 1,
            wins   = wins   + excluded.wins,
            losses = losses + excluded.losses
        """,
        (team_val, ha, hb, int(win), int(loss), season),
    )


def _upsert_trio(
    cursor: sqlite3.Cursor,
    team_val: str,
    ha: str,
    hb: str,
    hc: str,
    win: bool,
    loss: bool,
    season: str,
) -> None:
    ha, hb, hc = _canon_trio(ha, hb, hc)
    cursor.execute(
        """
        INSERT INTO de_trio_shell_stats
            (team_name, hero_a, hero_b, hero_c, co_appearances, wins, losses, season)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(team_name, hero_a, hero_b, hero_c, season) DO UPDATE SET
            co_appearances = co_appearances + 1,
            wins   = wins   + excluded.wins,
            losses = losses + excluded.losses
        """,
        (team_val, ha, hb, hc, int(win), int(loss), season),
    )


# ---------------------------------------------------------------------------
# Core ETL
# ---------------------------------------------------------------------------

def _process_map_record(
    cursor: sqlite3.Cursor,
    map_rec: dict,
    scrim_id: int,
    our_team: str,
    enemy_team: str,
    season: str,
) -> None:
    map_name = map_rec.get("map_name", "")
    our_team_slot = map_rec.get("our_team_slot", "")
    enemy_slot = "team2" if our_team_slot == "team1" else "team1"
    map_result = map_rec.get("result", "")
    map_score = map_rec.get("score", "")
    source_map_id = map_rec.get("id")

    draft = map_rec.get("draft") or {}
    comps = map_rec.get("comp") or []

    # For each comp snapshot (submap), insert a de_maps row + aggregations.
    # Filter out empty comp snapshots.
    valid_comps = [
        c for c in comps
        if any(_norm_hero(e.get("hero", "")) for slot in ["team1", "team2"]
               for e in c.get(slot, []))
    ]

    # If no valid comp snapshots exist we still insert one maps row for
    # draft action tracking.
    if not valid_comps:
        valid_comps = [{"submap": "", "side": "", "score": "", "team1": [], "team2": []}]

    for comp in valid_comps:
        submap = comp.get("submap", "")
        comp_side = comp.get("side", "")

        cursor.execute(
            """
            INSERT INTO de_maps
                (scrim_id, map_name, submap, side, our_team_slot, result, score,
                 season, enemy_team_name, team_name, source_map_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (scrim_id, map_name, submap, comp_side, our_team_slot, map_result,
             map_score, season, enemy_team, our_team, source_map_id),
        )
        de_map_id = cursor.lastrowid

        # Build per-slot hero lists from this comp snapshot
        comp_heroes: dict[str, list[str]] = {}
        for slot in ("team1", "team2"):
            comp_heroes[slot] = _heroes_in_comp_slot(comp.get(slot, []))

        # Draft actions (bans/protects/picks)
        action_rows = _extract_draft_actions(
            de_map_id, draft, scrim_id, map_name, season, enemy_team, comp_heroes
        )
        cursor.executemany(
            """
            INSERT INTO de_draft_actions
                (de_map_id, team_slot, action_type, action_order, hero,
                 scrim_id, map_name, season, enemy_team_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            action_rows,
        )

        # Pair / trio / bias aggregations per comp slot
        for slot in ("team1", "team2"):
            heroes = comp_heroes.get(slot, [])
            if not heroes:
                continue

            # Determine which team this slot represents
            if slot == our_team_slot:
                slot_team = our_team
                is_enemy = False
            else:
                slot_team = enemy_team
                is_enemy = True

            slot_result = _result_for_team(map_result, our_team_slot, slot)
            win = slot_result == "Win"
            loss = slot_result == "Loss"

            # team_hero_bias (played)
            for hero in heroes:
                if is_enemy:
                    bias_team = enemy_team
                else:
                    bias_team = our_team
                cursor.execute(
                    """
                    INSERT INTO de_team_hero_bias
                        (team_name, hero, played_count, played_wins, played_losses, season)
                    VALUES (?, ?, 1, ?, ?, ?)
                    ON CONFLICT(team_name, hero, season) DO UPDATE SET
                        played_count  = played_count  + 1,
                        played_wins   = played_wins   + excluded.played_wins,
                        played_losses = played_losses + excluded.played_losses
                    """,
                    (bias_team, hero, int(win), int(loss), season),
                )

            # ally / enemy pair stats
            for ha, hb in itertools.combinations(heroes, 2):
                if is_enemy:
                    _upsert_pair(cursor, "de_enemy_pair_stats", "enemy_team_name",
                                 enemy_team, ha, hb, win, loss, season)
                else:
                    _upsert_pair(cursor, "de_ally_pair_stats", "team_name",
                                 our_team, ha, hb, win, loss, season)

            # trio shell stats
            for trio in itertools.combinations(heroes, 3):
                if is_enemy:
                    _upsert_trio(cursor, enemy_team, *trio, win, loss, season)
                else:
                    _upsert_trio(cursor, our_team, *trio, win, loss, season)

            # team-up activations
            hero_set = set(heroes)
            for (tu_name, ta, tb, tc) in TEAM_UPS:
                if tc is None:
                    activated = {ta, tb}.issubset(hero_set)
                else:
                    activated = {ta, tb, tc}.issubset(hero_set)
                if activated:
                    cursor.execute(
                        """
                        INSERT INTO de_teamup_stats
                            (teamup_name, team_name, hero_a, hero_b, hero_c,
                             activations, wins, losses, season)
                        VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                        ON CONFLICT(teamup_name, team_name, season) DO UPDATE SET
                            activations = activations + 1,
                            wins   = wins   + excluded.wins,
                            losses = losses + excluded.losses
                        """,
                        (tu_name, slot_team, ta, tb, tc,
                         int(win), int(loss), season),
                    )

        # Player-hero appearances (only if player field is populated)
        for slot in ("team1", "team2"):
            if slot == our_team_slot:
                slot_team = our_team
            else:
                slot_team = enemy_team
            slot_result = _result_for_team(map_result, our_team_slot, slot)
            win = slot_result == "Win"
            loss = slot_result == "Loss"
            for entry in comp.get(slot, []):
                hero = _norm_hero(entry.get("hero", ""))
                player = (entry.get("player") or "").strip()
                if hero and player:
                    cursor.execute(
                        """
                        INSERT INTO de_player_heroes
                            (team_name, player_name, hero, appearances, wins, losses, season)
                        VALUES (?, ?, ?, 1, ?, ?, ?)
                        ON CONFLICT(team_name, player_name, hero, season) DO UPDATE SET
                            appearances = appearances + 1,
                            wins        = wins        + excluded.wins,
                            losses      = losses      + excluded.losses
                        """,
                        (slot_team, player, hero, int(win), int(loss), season),
                    )

    # Ban/protect bias aggregation (from draft dict, not comp)
    for slot in ("team1", "team2"):
        slot_actions = draft.get(slot) or {}
        if slot == our_team_slot:
            slot_team = our_team
        else:
            slot_team = enemy_team
        for key, hero in slot_actions.items():
            hero = _norm_hero(hero)
            if not hero:
                continue
            if key.startswith("ban"):
                cursor.execute(
                    """
                    INSERT INTO de_team_hero_bias (team_name, hero, ban_count, season)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(team_name, hero, season) DO UPDATE SET
                        ban_count = ban_count + 1
                    """,
                    (slot_team, hero, season),
                )
            elif key.startswith("protect"):
                cursor.execute(
                    """
                    INSERT INTO de_team_hero_bias (team_name, hero, protect_count, season)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(team_name, hero, season) DO UPDATE SET
                        protect_count = protect_count + 1
                    """,
                    (slot_team, hero, season),
                )


def run_etl(conn: sqlite3.Connection, reset: bool = False) -> dict[str, int]:
    """
    Load scrims and tournament_matches from app_state, populate de_ tables.

    Returns a dict with row counts for each table.
    """
    conn.execute("PRAGMA foreign_keys = ON")

    if reset:
        drop_draft_engine_tables(conn)

    create_draft_engine_tables(conn)

    # Load raw data
    rows = conn.execute(
        "SELECT state_key, state_value FROM app_state WHERE state_key IN ('scrims', 'tournament_matches')"
    ).fetchall()
    state = {r[0]: r[1] for r in rows}

    scrims: list[dict] = json.loads(state.get("scrims", "[]"))
    tourney_matches: list[dict] = json.loads(state.get("tournament_matches", "[]"))

    cursor = conn.cursor()

    # ---------- scrims ----------
    for scrim in scrims:
        if not isinstance(scrim, dict):
            continue
        scrim_id = scrim.get("id", 0)
        our_team = (scrim.get("team_name") or "").strip() or "Unknown"
        enemy_team = (scrim.get("enemy_team") or scrim.get("opponent") or "").strip() or "Unknown"
        season = str(scrim.get("season") or "")

        for map_rec in (scrim.get("maps") or []):
            if not isinstance(map_rec, dict):
                continue
            _process_map_record(cursor, map_rec, scrim_id, our_team, enemy_team, season)

    # ---------- tournament matches ----------
    for tourney in tourney_matches:
        if not isinstance(tourney, dict):
            continue
        season = str(tourney.get("season") or "")
        for match in (tourney.get("matches") or []):
            if not isinstance(match, dict):
                continue
            t1_name = (match.get("team1_name") or "").strip() or "Unknown"
            t2_name = (match.get("team2_name") or "").strip() or "Unknown"
            scrim_id = match.get("id", 0)
            for map_rec in (match.get("maps") or []):
                if not isinstance(map_rec, dict):
                    continue
                # our_team_slot tells us which name is "ours"
                our_team_slot = map_rec.get("our_team_slot", "team1")
                our_team = t1_name if our_team_slot == "team1" else t2_name
                enemy_team = t2_name if our_team_slot == "team1" else t1_name
                _process_map_record(cursor, map_rec, scrim_id, our_team, enemy_team, season)

    conn.commit()

    # Return row counts
    counts = {}
    for table in [
        "de_maps", "de_draft_actions", "de_player_heroes",
        "de_ally_pair_stats", "de_enemy_pair_stats", "de_team_hero_bias",
        "de_trio_shell_stats", "de_teamup_stats",
    ]:
        (n,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        counts[table] = n
    return counts


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _default_db_path() -> Path:
    env = os.environ.get("DATABASE_PATH") or os.environ.get("RENDER_DISK_MOUNT_PATH")
    if env:
        p = Path(env)
        if p.is_dir():
            return p / "rivals_stats.db"
        return p
    return Path(__file__).resolve().parent.parent / "rivals_stats.db"


def main() -> None:
    parser = argparse.ArgumentParser(description="Draft engine ETL")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database")
    parser.add_argument("--reset", action="store_true",
                        help="Drop and recreate all de_ tables before loading")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else _default_db_path()
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return

    print(f"ETL target: {db_path}  (reset={args.reset})")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        counts = run_etl(conn, reset=args.reset)
    finally:
        conn.close()

    print("Done. Row counts:")
    for table, n in counts.items():
        print(f"  {table:<30} {n:>6}")


if __name__ == "__main__":
    main()
