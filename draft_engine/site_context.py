"""
draft_engine/site_context.py
============================
Unified read-only query layer over the full site database.

This module is the primary knowledge source for the chatbot layer.
The draft engine (engine.py, coming in Stage 3) calls into this for
context.  Use this module directly when a question can be answered
from site data alone without simulation.

Resolution rules
----------------
* "we" / "our team" → the team where is_personal = 1 (currently Navi)
* A bare player name → resolved against the personal team's roster first,
  then against enemy rosters if not found.
* Season → defaults to the most recent season present in de_maps unless
  overridden.
* Enemy team name → fuzzy-matched (case-insensitive prefix) against teams
  table + enemy_team_name column in de_maps.

Public API
----------
All public functions accept a sqlite3.Connection.  They return plain dicts
or lists of dicts — no Row objects — so callers don't need the connection
to stay open.

    get_personal_team(conn)                 → {id, name, ...}
    get_current_season(conn)                → "7" | ""
    resolve_player(conn, name)              → {player_name, team_name, role, ...} | None
    resolve_team(conn, name)                → team_name str | None
    get_player_heroes(conn, player, season) → [{hero, appearances, wins, losses}, ...]
    get_hero_stats(conn, hero, team, season)→ {played, wins, losses, ban_count, protect_count, pairs, ...}
    get_team_overview(conn, team, season)   → {hero_bias, pair_cores, map_stats, ban_patterns}
    get_map_stats(conn, map_name, team, season) → {wins, losses, hero_freq, bans}
    get_scrim_history(conn, team, vs, season, limit) → [scrim dicts]
    get_enemy_draft_profile(conn, enemy, season)     → {bias, pairs, ban_patterns, trio_shells}
    search_site(conn, query, season)        → aggregated multi-section answer dict
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from typing import Any


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _fuzzy_match(name: str, candidates: list[str]) -> str | None:
    """Case-insensitive prefix or substring match. Returns best candidate or None."""
    n = _norm(name)
    # exact
    for c in candidates:
        if _norm(c) == n:
            return c
    # prefix
    for c in candidates:
        if _norm(c).startswith(n):
            return c
    # substring
    for c in candidates:
        if n in _norm(c):
            return c
    return None


def _load_scrims(conn: sqlite3.Connection) -> list[dict]:
    row = conn.execute(
        "SELECT state_value FROM app_state WHERE state_key = 'scrims'"
    ).fetchone()
    if not row:
        return []
    return json.loads(row[0] or "[]")


def _load_tournament_matches(conn: sqlite3.Connection) -> list[dict]:
    row = conn.execute(
        "SELECT state_value FROM app_state WHERE state_key = 'tournament_matches'"
    ).fetchone()
    if not row:
        return []
    return json.loads(row[0] or "[]")


def _all_map_records(scrims: list[dict]) -> list[dict]:
    """Flatten all map records from scrims into a list with scrim metadata attached."""
    out = []
    for scrim in scrims:
        for m in scrim.get("maps", []):
            m = dict(m)
            m["_scrim_id"] = scrim.get("id")
            m["_enemy_team"] = scrim.get("enemy_team") or scrim.get("opponent") or ""
            m["_team_name"] = scrim.get("team_name") or ""
            m["_season"] = str(scrim.get("season") or "")
            m["_scrim_date"] = scrim.get("scrim_date") or ""
            out.append(m)
    return out


def _all_tourney_map_records(tourney_matches: list[dict]) -> list[dict]:
    out = []
    for t in tourney_matches:
        season = str(t.get("season") or "")
        for match in t.get("matches", []):
            t1 = match.get("team1_name") or ""
            t2 = match.get("team2_name") or ""
            for m in match.get("maps", []):
                m = dict(m)
                our_slot = m.get("our_team_slot", "team1")
                m["_scrim_id"] = match.get("id")
                m["_team_name"] = t1 if our_slot == "team1" else t2
                m["_enemy_team"] = t2 if our_slot == "team1" else t1
                m["_season"] = season
                m["_scrim_date"] = match.get("scrim_date") or ""
                out.append(m)
    return out


# ---------------------------------------------------------------------------
# Core resolution
# ---------------------------------------------------------------------------

def get_personal_team(conn: sqlite3.Connection) -> dict:
    """Return the team row where is_personal = 1."""
    row = conn.execute(
        "SELECT id, name, notes, quality_tag FROM teams WHERE is_personal = 1 LIMIT 1"
    ).fetchone()
    if row:
        return dict(row)
    # fallback: first team
    row = conn.execute("SELECT id, name, notes, quality_tag FROM teams ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else {}


def get_current_season(conn: sqlite3.Connection) -> str:
    """Return the most recent non-empty season string seen in de_maps."""
    row = conn.execute(
        "SELECT season FROM de_maps WHERE season != '' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else ""


def _known_teams(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM teams ORDER BY name").fetchall()
    return [r[0] for r in rows]


def resolve_team(conn: sqlite3.Connection, name: str) -> str | None:
    """Fuzzy-resolve a team name string to the canonical name in the DB."""
    if not name:
        return None
    candidates = _known_teams(conn)
    # Also check enemy_team_name values from de_maps
    extra = [r[0] for r in conn.execute(
        "SELECT DISTINCT enemy_team_name FROM de_maps WHERE enemy_team_name != ''"
    ).fetchall()]
    return _fuzzy_match(name, candidates) or _fuzzy_match(name, extra)


def resolve_player(conn: sqlite3.Connection, name: str, prefer_personal: bool = True) -> dict | None:
    """
    Resolve a player name to {player_name, team_name, role, heroes}.
    Searches personal team first, then enemy rosters in de_player_heroes.
    Returns None if not found.
    """
    if not name:
        return None

    personal = get_personal_team(conn)
    personal_name = personal.get("name", "")

    # All known players from de_player_heroes (covers both sides)
    all_players = conn.execute(
        "SELECT DISTINCT player_name, team_name FROM de_player_heroes"
    ).fetchall()
    player_names = [r[0] for r in all_players]

    match = _fuzzy_match(name, player_names)
    if not match:
        return None

    # Prefer personal team if the same name appears on multiple teams
    team_rows = conn.execute(
        "SELECT DISTINCT team_name FROM de_player_heroes WHERE lower(player_name) = lower(?)",
        (match,),
    ).fetchall()
    teams_for_player = [r[0] for r in team_rows]

    if prefer_personal and personal_name in teams_for_player:
        chosen_team = personal_name
    else:
        chosen_team = teams_for_player[0] if teams_for_player else ""

    # Get their hero pool
    heroes = conn.execute(
        """
        SELECT hero, SUM(appearances) as appearances,
               SUM(wins) as wins, SUM(losses) as losses
        FROM de_player_heroes
        WHERE lower(player_name) = lower(?) AND team_name = ?
        GROUP BY hero ORDER BY appearances DESC
        """,
        (match, chosen_team),
    ).fetchall()

    # Role from players table
    role_row = conn.execute(
        """
        SELECT p.role FROM players p
        JOIN teams t ON p.team_id = t.id
        WHERE lower(p.name) = lower(?) AND t.name = ?
        """,
        (match, chosen_team),
    ).fetchone()

    return {
        "player_name": match,
        "team_name": chosen_team,
        "role": role_row[0] if role_row else "",
        "heroes": [dict(h) for h in heroes],
    }


# ---------------------------------------------------------------------------
# Hero queries
# ---------------------------------------------------------------------------

def get_player_heroes(
    conn: sqlite3.Connection,
    player_name: str,
    season: str | None = None,
) -> list[dict]:
    """Hero pool for a player, aggregated across seasons unless season is given."""
    resolved = resolve_player(conn, player_name)
    if not resolved:
        return []
    if season:
        rows = conn.execute(
            """
            SELECT hero, SUM(appearances) as appearances,
                   SUM(wins) as wins, SUM(losses) as losses
            FROM de_player_heroes
            WHERE lower(player_name) = lower(?) AND team_name = ? AND season = ?
            GROUP BY hero ORDER BY appearances DESC
            """,
            (resolved["player_name"], resolved["team_name"], season),
        ).fetchall()
    else:
        rows = resolved["heroes"]
    return [dict(r) for r in rows]


def get_hero_stats(
    conn: sqlite3.Connection,
    hero: str,
    team: str | None = None,
    season: str | None = None,
) -> dict:
    """
    Return played stats, ban/protect counts, and most common pair partners
    for a hero, optionally scoped to a team and season.
    """
    season_clause = "AND season = ?" if season else ""
    team_clause = "AND team_name = ?" if team else ""
    params_base: list[Any] = [hero]
    if team:
        params_base.append(team)
    if season:
        params_base.append(season)

    bias = conn.execute(
        f"""
        SELECT SUM(ban_count) as ban_count,
               SUM(protect_count) as protect_count,
               SUM(played_count) as played_count,
               SUM(played_wins) as played_wins,
               SUM(played_losses) as played_losses
        FROM de_team_hero_bias
        WHERE hero = ? {team_clause} {season_clause}
        """,
        params_base,
    ).fetchone()

    # Pair partners — from ally stats (our team) or enemy stats
    pair_params: list[Any] = [hero, hero]
    if team:
        pair_params = [team, hero, hero]
    if season:
        pair_params.append(season)

    if team:
        pair_rows = conn.execute(
            f"""
            SELECT CASE WHEN hero_a = ? THEN hero_b ELSE hero_a END as partner,
                   SUM(co_appearances) as co_appearances,
                   SUM(wins) as wins
            FROM de_ally_pair_stats
            WHERE team_name = ? AND (hero_a = ? OR hero_b = ?) {season_clause}
            GROUP BY partner ORDER BY co_appearances DESC LIMIT 10
            """,
            ([hero, team, hero, hero] + ([season] if season else [])),
        ).fetchall()
    else:
        pair_rows = conn.execute(
            f"""
            SELECT CASE WHEN hero_a = ? THEN hero_b ELSE hero_a END as partner,
                   SUM(co_appearances) as co_appearances,
                   SUM(wins) as wins
            FROM de_ally_pair_stats
            WHERE hero_a = ? OR hero_b = ?
            {season_clause}
            GROUP BY partner ORDER BY co_appearances DESC LIMIT 10
            """,
            ([hero, hero, hero] + ([season] if season else [])),
        ).fetchall()

    return {
        "hero": hero,
        "team": team,
        "season": season,
        "ban_count": (bias["ban_count"] or 0) if bias else 0,
        "protect_count": (bias["protect_count"] or 0) if bias else 0,
        "played_count": (bias["played_count"] or 0) if bias else 0,
        "played_wins": (bias["played_wins"] or 0) if bias else 0,
        "played_losses": (bias["played_losses"] or 0) if bias else 0,
        "pair_partners": [dict(r) for r in pair_rows],
    }


# ---------------------------------------------------------------------------
# Team overview
# ---------------------------------------------------------------------------

def get_team_overview(
    conn: sqlite3.Connection,
    team: str | None = None,
    season: str | None = None,
) -> dict:
    """
    Return a structured overview of a team's draft tendencies:
    hero bias (ban/protect/play), pair cores, map win rates, ban patterns.
    """
    if team is None:
        team = get_personal_team(conn).get("name", "")

    season_clause = "AND season = ?" if season else ""
    params = [team] + ([season] if season else [])

    bias_rows = conn.execute(
        f"""
        SELECT hero, ban_count, protect_count, played_count,
               played_wins, played_losses
        FROM de_team_hero_bias
        WHERE team_name = ? {season_clause}
        ORDER BY (ban_count + protect_count + played_count) DESC
        """,
        params,
    ).fetchall()

    pair_rows = conn.execute(
        f"""
        SELECT hero_a, hero_b, co_appearances, wins, losses
        FROM de_ally_pair_stats
        WHERE team_name = ? {season_clause}
        ORDER BY co_appearances DESC LIMIT 20
        """,
        params,
    ).fetchall()

    map_rows = conn.execute(
        f"""
        SELECT map_name, COUNT(*) as played,
               SUM(result = 'Win') as wins,
               SUM(result = 'Loss') as losses
        FROM de_maps
        WHERE team_name = ? {season_clause}
        GROUP BY map_name ORDER BY played DESC
        """,
        params,
    ).fetchall()

    return {
        "team": team,
        "season": season,
        "hero_bias": [dict(r) for r in bias_rows],
        "pair_cores": [dict(r) for r in pair_rows],
        "map_stats": [dict(r) for r in map_rows],
    }


# ---------------------------------------------------------------------------
# Map queries
# ---------------------------------------------------------------------------

def get_map_stats(
    conn: sqlite3.Connection,
    map_name: str,
    team: str | None = None,
    season: str | None = None,
) -> dict:
    """Win/loss record and hero frequency for a map, optionally scoped to team/season."""
    if team is None:
        team = get_personal_team(conn).get("name", "")

    season_clause = "AND m.season = ?" if season else ""
    params: list[Any] = [map_name, team] + ([season] if season else [])

    record = conn.execute(
        f"""
        SELECT COUNT(*) as played,
               SUM(result = 'Win') as wins,
               SUM(result = 'Loss') as losses
        FROM de_maps m
        WHERE map_name = ? AND team_name = ? {season_clause}
        """,
        params,
    ).fetchone()

    # Hero frequency on this map for this team (from draft actions - picks)
    hero_freq = conn.execute(
        f"""
        SELECT da.hero, COUNT(*) as count
        FROM de_draft_actions da
        JOIN de_maps m ON da.de_map_id = m.id
        WHERE m.map_name = ? AND m.team_name = ? AND da.action_type = 'pick'
          AND da.team_slot = m.our_team_slot {season_clause}
        GROUP BY da.hero ORDER BY count DESC LIMIT 15
        """,
        params,
    ).fetchall()

    # Bans on this map
    bans = conn.execute(
        f"""
        SELECT da.hero, COUNT(*) as count
        FROM de_draft_actions da
        JOIN de_maps m ON da.de_map_id = m.id
        WHERE m.map_name = ? AND m.team_name = ? AND da.action_type = 'ban'
          AND da.team_slot = m.our_team_slot {season_clause}
        GROUP BY da.hero ORDER BY count DESC LIMIT 10
        """,
        params,
    ).fetchall()

    return {
        "map_name": map_name,
        "team": team,
        "season": season,
        "played": record["played"] if record else 0,
        "wins": record["wins"] if record else 0,
        "losses": record["losses"] if record else 0,
        "hero_freq": [dict(r) for r in hero_freq],
        "bans": [dict(r) for r in bans],
    }


# ---------------------------------------------------------------------------
# Scrim history
# ---------------------------------------------------------------------------

def get_scrim_history(
    conn: sqlite3.Connection,
    team: str | None = None,
    vs: str | None = None,
    season: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    Return recent scrim records (from raw JSON) optionally filtered by
    opponent and/or season.  Enriched with map-level results.
    """
    if team is None:
        team = get_personal_team(conn).get("name", "")

    scrims = _load_scrims(conn)
    out = []
    for scrim in scrims:
        t = (scrim.get("team_name") or "").strip()
        if _norm(t) != _norm(team):
            continue
        s = str(scrim.get("season") or "")
        if season and _norm(s) != _norm(season):
            continue
        opp = (scrim.get("enemy_team") or scrim.get("opponent") or "").strip()
        if vs and not (_norm(vs) in _norm(opp) or _norm(opp) in _norm(vs)):
            continue
        out.append(scrim)

    # Sort newest first by scrim_date
    out.sort(key=lambda x: x.get("scrim_date") or "", reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# Enemy draft profile
# ---------------------------------------------------------------------------

def get_enemy_draft_profile(
    conn: sqlite3.Connection,
    enemy: str,
    season: str | None = None,
) -> dict:
    """
    Everything the engine knows about an enemy team's draft tendencies:
    hero bias, pair cores, trio shells, ban patterns, player hero pools.
    """
    resolved_enemy = resolve_team(conn, enemy) or enemy
    season_clause = "AND season = ?" if season else ""
    params = [resolved_enemy] + ([season] if season else [])

    bias = conn.execute(
        f"""
        SELECT hero, ban_count, protect_count, played_count,
               played_wins, played_losses
        FROM de_team_hero_bias
        WHERE team_name = ? {season_clause}
        ORDER BY (ban_count + protect_count + played_count) DESC
        """,
        params,
    ).fetchall()

    pairs = conn.execute(
        f"""
        SELECT hero_a, hero_b, co_appearances, wins, losses
        FROM de_enemy_pair_stats
        WHERE enemy_team_name = ? {season_clause}
        ORDER BY co_appearances DESC LIMIT 20
        """,
        params,
    ).fetchall()

    trios = conn.execute(
        f"""
        SELECT hero_a, hero_b, hero_c, co_appearances, wins, losses
        FROM de_trio_shell_stats
        WHERE team_name = ? {season_clause}
        ORDER BY co_appearances DESC LIMIT 20
        """,
        params,
    ).fetchall()

    # Ban targets: what does the enemy ban?
    ban_targets = conn.execute(
        f"""
        SELECT hero, COUNT(*) as count
        FROM de_draft_actions
        WHERE enemy_team_name = ? AND action_type = 'ban'
          AND team_slot != (
              SELECT our_team_slot FROM de_maps WHERE id = de_draft_actions.de_map_id LIMIT 1
          ) {season_clause}
        GROUP BY hero ORDER BY count DESC LIMIT 10
        """,
        params,
    ).fetchall()

    players = conn.execute(
        f"""
        SELECT player_name, hero, SUM(appearances) as appearances,
               SUM(wins) as wins
        FROM de_player_heroes
        WHERE team_name = ? {season_clause}
        GROUP BY player_name, hero
        ORDER BY player_name, appearances DESC
        """,
        params,
    ).fetchall()

    return {
        "enemy_team": resolved_enemy,
        "season": season,
        "hero_bias": [dict(r) for r in bias],
        "pair_cores": [dict(r) for r in pairs],
        "trio_shells": [dict(r) for r in trios],
        "ban_targets": [dict(r) for r in ban_targets],
        "player_pools": [dict(r) for r in players],
    }


# ---------------------------------------------------------------------------
# Wide site search
# ---------------------------------------------------------------------------

def search_site(
    conn: sqlite3.Connection,
    query: str,
    season: str | None = None,
) -> dict:
    """
    Multi-section query.  Detects hero names, player names, team names,
    and map names in `query` and returns evidence from each matching section.

    Returns a dict with keys:
      players, heroes, teams, maps, scrims_summary
    """
    personal = get_personal_team(conn)
    personal_name = personal.get("name", "")
    q = _norm(query)

    result: dict[str, Any] = {
        "query": query,
        "personal_team": personal_name,
        "season": season,
        "players": [],
        "heroes": [],
        "teams": [],
        "maps": [],
        "scrims_summary": [],
        "sources_used": [],
    }

    # --- player match ---
    all_player_names = [r[0] for r in conn.execute(
        "SELECT DISTINCT player_name FROM de_player_heroes"
    ).fetchall()]
    matched_players = [p for p in all_player_names if _norm(p) in q or q in _norm(p)]
    for p in matched_players:
        info = resolve_player(conn, p)
        if info:
            info["heroes"] = get_player_heroes(conn, p, season)
            result["players"].append(info)
    if matched_players:
        result["sources_used"].append("player_heroes")

    # --- hero match ---
    all_heroes = [r[0] for r in conn.execute(
        "SELECT DISTINCT hero FROM de_team_hero_bias"
    ).fetchall()]
    matched_heroes = [h for h in all_heroes if _norm(h) in q or q in _norm(h)]
    for h in matched_heroes:
        result["heroes"].append(get_hero_stats(conn, h, personal_name, season))
    if matched_heroes:
        result["sources_used"].append("hero_stats")

    # --- team match ---
    all_teams = _known_teams(conn)
    matched_teams = [t for t in all_teams if _norm(t) in q or q in _norm(t)]
    for t in matched_teams:
        result["teams"].append(get_team_overview(conn, t, season))
    if matched_teams:
        result["sources_used"].append("team_overview")

    # --- map match ---
    all_maps = [r[0] for r in conn.execute(
        "SELECT DISTINCT map_name FROM de_maps"
    ).fetchall()]
    matched_maps = [m for m in all_maps if _norm(m) in q or q in _norm(m)]
    for m in matched_maps:
        result["maps"].append(get_map_stats(conn, m, personal_name, season))
    if matched_maps:
        result["sources_used"].append("map_stats")

    # --- scrim history snippet ---
    if matched_teams or "scrim" in q or "history" in q:
        vs = matched_teams[0] if matched_teams and matched_teams[0] != personal_name else None
        result["scrims_summary"] = get_scrim_history(conn, personal_name, vs=vs, season=season, limit=5)
        result["sources_used"].append("scrim_history")

    if not result["sources_used"]:
        result["sources_used"].append("none — no matching entity found in site data")

    return result


# ---------------------------------------------------------------------------
# Convenience: open DB from default path
# ---------------------------------------------------------------------------

def open_db(path: str | None = None) -> sqlite3.Connection:
    import os
    from pathlib import Path
    if path:
        db_path = Path(path)
    else:
        env = os.environ.get("DATABASE_PATH") or os.environ.get("RENDER_DISK_MOUNT_PATH")
        if env:
            p = Path(env)
            db_path = p / "rivals_stats.db" if p.is_dir() else p
        else:
            db_path = Path(__file__).resolve().parent.parent / "rivals_stats.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn
