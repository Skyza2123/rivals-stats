"""
Stage 1 — Draft engine schema.

Creates (or migrates) the draft engine tables inside the existing SQLite
database.  All tables live alongside the app tables; they are prefixed
`de_` to avoid collisions.

Tables
------
de_maps
    One row per submap played.  A submap is the atomic unit where a hero
    lineup was actually fielded.

de_draft_actions
    Every ban, protect, and pick action extracted from a map's draft dict.
    The order column encodes the canonical ban/protect/pick sequence.

de_player_heroes
    Aggregated hero appearances per player per team, derived from comp
    records where the player field is populated.  When player names are
    absent (most older records) this table is sparse; it fills in as
    player assignments are back-filled in the UI.

de_ally_pair_stats
    How often hero_a and hero_b appeared in the same lineup (same team,
    same comp snapshot).  Counted per team so you can distinguish "our
    team" pair tendencies from general population tendencies.

de_enemy_pair_stats
    Same as ally pairs but sourced from the opposing slot.  Stored
    separately so you never need to join on team identity at query time.

de_team_hero_bias
    Per-team frequency that a hero appeared in a draft action (ban,
    protect, or pick) or in a played comp.  This drives prior probability
    in the likely-next-pick distribution.

de_trio_shell_stats
    Three-hero combinations observed in a single comp.  Restricted to
    combinations of size exactly 3 (C(6,3)=20 per comp) to keep the
    table tractable while still capturing shell identity.

de_teamup_stats
    Tracks activations of known team-up bonuses: how often the required
    heroes appeared together in the same comp, and how often the combo
    appeared on a winning or losing side.
"""

import sqlite3
from pathlib import Path


SCHEMA_SQL = """
-- ---------------------------------------------------------------------------
-- de_maps
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS de_maps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scrim_id        INTEGER NOT NULL,   -- foreign key to app scrims (logical)
    map_name        TEXT    NOT NULL,
    submap          TEXT    NOT NULL DEFAULT '',
    side            TEXT    NOT NULL DEFAULT '',  -- 'Attack' | 'Defense' | ''
    our_team_slot   TEXT    NOT NULL DEFAULT '',  -- 'team1' | 'team2'
    result          TEXT    NOT NULL DEFAULT '',  -- 'Win' | 'Loss' | ''
    score           TEXT    NOT NULL DEFAULT '',
    season          TEXT    NOT NULL DEFAULT '',
    enemy_team_name TEXT    NOT NULL DEFAULT '',
    team_name       TEXT    NOT NULL DEFAULT '',
    source_map_id   INTEGER,            -- map.id from the scrim JSON if present
    created_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- de_draft_actions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS de_draft_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    de_map_id       INTEGER NOT NULL REFERENCES de_maps(id) ON DELETE CASCADE,
    team_slot       TEXT    NOT NULL,   -- 'team1' | 'team2'
    action_type     TEXT    NOT NULL,   -- 'ban' | 'protect' | 'pick'
    action_order    INTEGER NOT NULL,   -- 1-based global order within the draft
    hero            TEXT    NOT NULL,
    -- denormalised for cheap query
    scrim_id        INTEGER NOT NULL,
    map_name        TEXT    NOT NULL,
    season          TEXT    NOT NULL DEFAULT '',
    enemy_team_name TEXT    NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------------------
-- de_player_heroes
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS de_player_heroes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name       TEXT    NOT NULL,
    player_name     TEXT    NOT NULL,
    hero            TEXT    NOT NULL,
    appearances     INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    season          TEXT    NOT NULL DEFAULT '',
    UNIQUE (team_name, player_name, hero, season)
);

-- ---------------------------------------------------------------------------
-- de_ally_pair_stats
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS de_ally_pair_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name       TEXT    NOT NULL,
    hero_a          TEXT    NOT NULL,
    hero_b          TEXT    NOT NULL,
    co_appearances  INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    season          TEXT    NOT NULL DEFAULT '',
    UNIQUE (team_name, hero_a, hero_b, season),
    CHECK (hero_a <= hero_b)  -- canonical order prevents duplicates
);

-- ---------------------------------------------------------------------------
-- de_enemy_pair_stats
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS de_enemy_pair_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    enemy_team_name TEXT    NOT NULL,
    hero_a          TEXT    NOT NULL,
    hero_b          TEXT    NOT NULL,
    co_appearances  INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,  -- wins for the ENEMY
    losses          INTEGER NOT NULL DEFAULT 0,
    season          TEXT    NOT NULL DEFAULT '',
    UNIQUE (enemy_team_name, hero_a, hero_b, season),
    CHECK (hero_a <= hero_b)
);

-- ---------------------------------------------------------------------------
-- de_team_hero_bias
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS de_team_hero_bias (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name       TEXT    NOT NULL,
    hero            TEXT    NOT NULL,
    -- draft phase counts
    ban_count       INTEGER NOT NULL DEFAULT 0,
    protect_count   INTEGER NOT NULL DEFAULT 0,
    -- played counts (from comp snapshots)
    played_count    INTEGER NOT NULL DEFAULT 0,
    played_wins     INTEGER NOT NULL DEFAULT 0,
    played_losses   INTEGER NOT NULL DEFAULT 0,
    season          TEXT    NOT NULL DEFAULT '',
    UNIQUE (team_name, hero, season)
);

-- ---------------------------------------------------------------------------
-- de_trio_shell_stats
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS de_trio_shell_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name       TEXT    NOT NULL,
    hero_a          TEXT    NOT NULL,
    hero_b          TEXT    NOT NULL,
    hero_c          TEXT    NOT NULL,
    co_appearances  INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    season          TEXT    NOT NULL DEFAULT '',
    UNIQUE (team_name, hero_a, hero_b, hero_c, season),
    CHECK (hero_a <= hero_b AND hero_b <= hero_c)
);

-- ---------------------------------------------------------------------------
-- de_teamup_stats
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS de_teamup_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    teamup_name     TEXT    NOT NULL,   -- human-readable name, e.g. "Voltaic Vision"
    team_name       TEXT    NOT NULL,
    hero_a          TEXT    NOT NULL,
    hero_b          TEXT    NOT NULL,
    hero_c          TEXT,               -- NULL when the team-up only needs two heroes
    activations     INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    season          TEXT    NOT NULL DEFAULT '',
    UNIQUE (teamup_name, team_name, season)
);

-- ---------------------------------------------------------------------------
-- Indexes for common query patterns
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_de_draft_actions_enemy
    ON de_draft_actions (enemy_team_name, hero, action_type);

CREATE INDEX IF NOT EXISTS idx_de_ally_pair_team
    ON de_ally_pair_stats (team_name, hero_a, hero_b);

CREATE INDEX IF NOT EXISTS idx_de_enemy_pair_team
    ON de_enemy_pair_stats (enemy_team_name, hero_a, hero_b);

CREATE INDEX IF NOT EXISTS idx_de_team_hero_bias
    ON de_team_hero_bias (team_name, hero);

CREATE INDEX IF NOT EXISTS idx_de_trio_shell
    ON de_trio_shell_stats (team_name, hero_a, hero_b, hero_c);
"""


def create_draft_engine_tables(conn: sqlite3.Connection) -> None:
    """
    Idempotently create all draft engine tables.
    Safe to call on an existing database — uses CREATE TABLE IF NOT EXISTS.
    """
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def drop_draft_engine_tables(conn: sqlite3.Connection) -> None:
    """
    Drop all de_ tables.  Used during a full re-ETL to start clean.
    """
    tables = [
        "de_teamup_stats",
        "de_trio_shell_stats",
        "de_team_hero_bias",
        "de_enemy_pair_stats",
        "de_ally_pair_stats",
        "de_player_heroes",
        "de_draft_actions",
        "de_maps",
    ]
    for table in tables:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


if __name__ == "__main__":
    # Quick smoke-test: create the tables in an in-memory DB and print them.
    conn = sqlite3.connect(":memory:")
    create_draft_engine_tables(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'de_%' ORDER BY name"
    ).fetchall()
    for r in rows:
        print(r[0])
    conn.close()
