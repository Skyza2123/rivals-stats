# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

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


