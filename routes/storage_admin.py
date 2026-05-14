# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

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


@app.route("/db/dump-json/download", methods=["POST"])
def db_dump_json_download():
    """Create a fresh JSON dump and return it as a downloadable file."""
    try:
        save_app_state()
        dump_path = create_manual_json_dump()
        payload = dump_path.read_bytes()
    except Exception as exc:
        next_url = (request.form.get("next") or "").strip()
        if not next_url.startswith("/"):
            next_url = url_for("teams")
        flash(f"Database dump download failed: {exc}", "error")
        return redirect(next_url)

    return Response(
        payload,
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{dump_path.name}"',
            "Cache-Control": "no-store",
        },
    )


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
                
                # Skip backup history - not needed for restore and very large
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


# CSV column indices
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

# Build a lookup: lowercase submap name -> parent map name (e.g. "frozen airfield" -> "Hell's Haven")
_SUBMAP_PARENT: dict[str, str] = {}
for _parent, _subs in MAP_SUBMAPS.items():
    for _s in _subs:
        _SUBMAP_PARENT[_s.lower()] = _parent


