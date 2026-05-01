# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

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


def _ensure_team_sort_order_column(db: sqlite3.Connection) -> None:
    team_columns = {row[1] for row in db.execute("PRAGMA table_info(teams)").fetchall()}
    if "sort_order" not in team_columns:
        db.execute("ALTER TABLE teams ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
        db.commit()


@app.route("/teams")
def teams():
    db = get_db()
    migrate_enemy_teams_to_team_database(db)
    _ensure_team_sort_order_column(db)
    personal_rows = db.execute(
        "SELECT id, name FROM teams WHERE is_personal = 1 ORDER BY COALESCE(sort_order, 0), name COLLATE NOCASE"
    ).fetchall()
    if len(personal_rows) > 1:
        keep_personal_id = int(personal_rows[0]["id"])
        db.execute("UPDATE teams SET is_personal = 0 WHERE id != ?", (keep_personal_id,))
        db.commit()
        personal_rows = [personal_rows[0]]
    personal_team = personal_rows[0] if personal_rows else None

    selected_sort = (request.args.get("sort") or "quality").strip().lower()
    if selected_sort == "custom":
        selected_sort = "quality"
    if selected_sort not in {"quality", "win_rate", "last_played", "name"}:
        selected_sort = "quality"
    selected_view = (request.args.get("view") or "rows").strip().lower()
    if selected_view not in {"rows", "boxes"}:
        selected_view = "rows"
    team_rows = db.execute(
        """
        SELECT
            t.id,
            t.name,
            t.notes,
            t.quality_tag,
            COALESCE(t.sort_order, 0) AS sort_order,
            t.logo_path,
            t.is_personal,
            COUNT(p.id) AS player_count
        FROM teams t
        LEFT JOIN players p ON p.team_id = t.id
        GROUP BY t.id
        ORDER BY COALESCE(t.sort_order, 0), t.name COLLATE NOCASE
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
    staff_roles = {"Coach", "AC", "Analyst"}
    quality_rank = {"Preferred": 0, "Semi Preferred": 1, "Good": 2, "Avoid": 3}
    for row in team_rows:
        all_team_scrims = get_scrims_for_team(row["id"], row["name"])
        team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
        stats_scrims = team_scrims
        if personal_team is not None and int(row["id"]) != int(personal_team["id"]):
            personal_scrims = filter_scrims_by_season(
                get_scrims_for_team(int(personal_team["id"]), personal_team["name"]),
                selected_season,
            )
            stats_scrims = [
                scrim for scrim in personal_scrims
                if scrim_involves_team(scrim, int(row["id"]), row["name"])
            ]
        team_maps = sum(len(scrim.get("maps", [])) for scrim in stats_scrims)
        team_wins = sum(
            1
            for scrim in stats_scrims
            for map_entry in scrim.get("maps", [])
            if get_map_outcome_for_slot(map_entry, map_entry.get("our_team_slot", "team1")) == "Win"
        )
        team_win_rate = round((team_wins / team_maps) * 100, 1) if team_maps else 0
        if team_maps <= 0:
            win_rate_class = "wr-empty"
        elif team_win_rate >= 60:
            win_rate_class = "wr-good"
        elif team_win_rate >= 45:
            win_rate_class = "wr-ok"
        else:
            win_rate_class = "wr-bad"

        dated_scrims = []
        for scrim in stats_scrims:
            parsed_date = _parse_scrim_date(scrim.get("scrim_date", ""))
            if parsed_date is not None:
                dated_scrims.append((parsed_date, scrim.get("scrim_date", "")))
        last_played = ""
        last_played_sort = ""
        if dated_scrims:
            latest_date, latest_raw = max(dated_scrims, key=lambda item: item[0])
            last_played = latest_date.strftime("%m/%d/%Y")
            last_played_sort = latest_date.isoformat()

        roster_rows = db.execute(
            """
            SELECT name, role, COALESCE(is_sub, 0) AS is_sub
            FROM players
            WHERE team_id = ?
            ORDER BY
                CASE
                    WHEN role = 'Coach' THEN 10
                    WHEN role = 'AC' THEN 11
                    WHEN role = 'Analyst' THEN 12
                    WHEN COALESCE(is_sub, 0) = 0 THEN 0
                    ELSE 1
                END,
                name COLLATE NOCASE
            """,
            (row["id"],),
        ).fetchall()
        active_roster = [
            {"name": (player["name"] or "").strip(), "role": (player["role"] or "").strip(), "is_sub": bool(player["is_sub"])}
            for player in roster_rows
            if (player["name"] or "").strip() and (player["role"] or "").strip() not in staff_roles
        ]
        roster_by_role = {
            "Vanguard": [player for player in active_roster if player["role"] == "Vanguard"],
            "Duelist": [player for player in active_roster if player["role"] == "Duelist"],
            "Strategist": [player for player in active_roster if player["role"] == "Strategist"],
            "Flex / Other": [player for player in active_roster if player["role"] not in {"Vanguard", "Duelist", "Strategist"}],
        }
        staff = [
            {"name": (player["name"] or "").strip(), "role": (player["role"] or "").strip()}
            for player in roster_rows
            if (player["name"] or "").strip() and (player["role"] or "").strip() in staff_roles
        ]

        # Calculate hero pool (top 5 heroes)
        pick_counter: Counter = Counter()
        for scrim in stats_scrims:
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
                "quality_rank": quality_rank.get(row["quality_tag"], 99),
                "sort_order": int(row["sort_order"] or 0),
                "logo_path": row["logo_path"],
                "is_personal": bool(row["is_personal"]),
                "player_count": row["player_count"],
                "active_roster": active_roster,
                "roster_by_role": roster_by_role,
                "active_roster_count": len(active_roster),
                "staff": staff,
                "scrim_count": len(team_scrims),
                "stats_context": f"Our WR vs {row['name']}" if personal_team is not None and int(row["id"]) != int(personal_team["id"]) else "Overall",
                "map_count": team_maps,
                "map_win_rate": team_win_rate,
                "win_rate_class": win_rate_class,
                "last_played": last_played,
                "last_played_sort": last_played_sort,
                "hero_pool": hero_pool,
            }
        )

    if selected_sort == "win_rate":
        teams_with_scrim_stats.sort(key=lambda team: (team["map_count"] > 0, team["map_win_rate"], team["map_count"], team["name"].lower()), reverse=True)
    elif selected_sort == "last_played":
        teams_with_scrim_stats.sort(key=lambda team: (team["last_played_sort"], team["name"].lower()), reverse=True)
    elif selected_sort == "name":
        teams_with_scrim_stats.sort(key=lambda team: team["name"].lower())
    else:
        teams_with_scrim_stats.sort(key=lambda team: (team["quality_rank"], team["map_count"] <= 0, team["map_win_rate"], team["sort_order"], team["name"].lower()))

    # Personal team always floats to the top regardless of sort
    teams_with_scrim_stats.sort(key=lambda t: not t["is_personal"])

    personal_teams = [team for team in teams_with_scrim_stats if team["is_personal"]]

    return render_template(
        "teams.html",
        teams=teams_with_scrim_stats,
        personal_teams=personal_teams,
        season_options=season_options,
        selected_season=selected_season,
        selected_sort=selected_sort,
        selected_view=selected_view,
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
    _ensure_team_sort_order_column(db)
    raw_name = request.form.get("name", "")
    name = normalize_player_name(raw_name)
    notes = request.form.get("notes", "").strip()
    quality_tag_raw = " ".join(request.form.get("quality_tag", "").strip().split())[:32]
    quality_tag = quality_tag_raw if quality_tag_raw in TEAM_QUALITY_TAG_OPTIONS else ""
    try:
        sort_order = int(request.form.get("sort_order", "0") or 0)
    except ValueError:
        sort_order = 0
    logo_path = save_team_logo(request.files.get("logo"), name)
    is_personal = 1 if request.form.get("is_personal", "").strip() == "1" else 0

    if not name:
        flash("Team name is required.", "error")
        return redirect(url_for("teams"))

    try:
        if is_personal:
            db.execute("UPDATE teams SET is_personal = 0")
        db.execute(
            "INSERT INTO teams (name, notes, quality_tag, sort_order, logo_path, is_personal) VALUES (?, ?, ?, ?, ?, ?)",
            (name, notes, quality_tag, sort_order, logo_path, is_personal),
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
    _ensure_team_sort_order_column(db)
    current = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if current is None:
        abort(404)

    raw_name = request.form.get("name", "")
    name = normalize_player_name(raw_name)
    notes = request.form.get("notes", "").strip()
    quality_tag_raw = " ".join(request.form.get("quality_tag", "").strip().split())[:32]
    quality_tag = quality_tag_raw if quality_tag_raw in TEAM_QUALITY_TAG_OPTIONS else ""
    current_sort_order = current["sort_order"] if "sort_order" in current.keys() else 0
    try:
        sort_order = int(request.form.get("sort_order", "0") or current_sort_order or 0)
    except ValueError:
        sort_order = 0
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
        if is_personal:
            db.execute("UPDATE teams SET is_personal = 0 WHERE id != ?", (team_id,))
        db.execute(
            "UPDATE teams SET name = ?, notes = ?, quality_tag = ?, sort_order = ?, logo_path = ?, is_personal = ? WHERE id = ?",
            (name, notes, quality_tag, sort_order, logo_path, is_personal, team_id),
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
    _ensure_team_sort_order_column(db)
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

    if next_value:
        db.execute("UPDATE teams SET is_personal = 0 WHERE id != ?", (team_id,))
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

    staff_roles = {"Coach", "AC", "Analyst"}
    staff_role_options = ["Coach", "AC", "Analyst"]
    all_player_rows = db.execute(
        "SELECT * FROM players WHERE team_id = ? ORDER BY is_sub ASC, name COLLATE NOCASE",
        (team_id,),
    ).fetchall()
    player_rows = [row for row in all_player_rows if (row["role"] or "").strip() not in staff_roles]
    staff_rows = [row for row in all_player_rows if (row["role"] or "").strip() in staff_roles]

    team_analytics = build_scrim_analytics(
        team_scrims,
        roster_player_names=[row["name"] for row in player_rows],
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
    activity_heatmap = map_overview["activity_heatmap"]

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

    staff_members = [
        {
            "id": row["id"],
            "name": row["name"],
            "role": row["role"],
            "is_sub": bool(row["is_sub"]) if "is_sub" in row.keys() else False,
            "main_hero": row["main_hero"],
            "notes": row["notes"],
        }
        for row in staff_rows
    ]

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
        staff_members=staff_members,
        staff_roles=staff_role_options,
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
        activity_heatmap=activity_heatmap,
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

    # Sync roster from DB (same pattern as tournament_detail) so imported
    # tournament teams use the canonical database team when names match.
    if sync_tournament_team_with_db(tournament_team):
        normalize_tournament_record(tournament_record)
        tournament_team = get_tournament_team_by_id(tournament_record, tournament_team_id) or tournament_team
        save_app_state()

    source_team = None
    source_team_id = tournament_team.get("source_team_id")
    if isinstance(source_team_id, int):
        source_team = get_db().execute("SELECT * FROM teams WHERE id = ?", (source_team_id,)).fetchone()
    if source_team is None:
        source_team = get_db().execute(
            "SELECT * FROM teams WHERE lower(name) = lower(?)",
            ((tournament_team.get("name") or "").strip(),),
        ).fetchone()

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
    staff_roles = {"Coach", "AC", "Analyst"}
    players = []
    staff_members = []
    if source_team is not None:
        source_player_rows = get_db().execute(
            "SELECT * FROM players WHERE team_id = ? ORDER BY is_sub ASC, name COLLATE NOCASE",
            (source_team["id"],),
        ).fetchall()
        for player_row in source_player_rows:
            if (player_row["role"] or "").strip() in staff_roles:
                staff_members.append(
                    {
                        "id": player_row["id"],
                        "name": player_row["name"],
                        "role": player_row["role"],
                        "is_sub": bool(player_row["is_sub"]) if "is_sub" in player_row.keys() else False,
                        "main_hero": player_row["main_hero"],
                        "notes": player_row["notes"],
                    }
                )
                continue
            stats = compute_player_stats(player_row["name"], team_scrims)
            players.append(
                {
                    "id": player_row["id"],
                    "name": player_row["name"],
                    "role": player_row["role"],
                    "is_sub": bool(player_row["is_sub"]) if "is_sub" in player_row.keys() else False,
                    "main_hero": player_row["main_hero"],
                    "top_hero": stats.get("top_hero"),
                    "notes": player_row["notes"],
                    "stats": stats,
                }
            )
    else:
        players = [
            {
                "id": None,
                "name": player_name,
                "role": "",
                "is_sub": False,
                "main_hero": "",
                "top_hero": "",
                "notes": "",
                "stats": compute_player_stats(player_name, team_scrims),
            }
            for player_name in tournament_team.get("players", [])
        ]

    return render_template(
        "tournament_team_detail.html",
        tournament=tournament_record,
        tournament_team=tournament_team,
        source_team=source_team,
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
        staff_members=staff_members,
        hero_transformations=HERO_TRANSFORMATIONS,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
        map_images=MAP_IMAGES,
    )


