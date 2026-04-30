# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

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


