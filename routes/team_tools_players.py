# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

@app.route("/teams/<int:team_id>/prep-fragment")
def team_prep_fragment(team_id: int):
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
        selected_map_type=selected_map_type,
        **prep_context,
    )


@app.route("/teams/<int:team_id>/draft-predict")
def team_draft_predict(team_id: int):
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
    predictor_inputs = {
        field_key: (request.args.get(field_key) or "").strip()
        for field_key in PREDICTOR_INPUT_ORDER
    }
    return jsonify(build_draft_predictor(team_scrims, predictor_inputs))


@app.route("/teams/<int:team_id>/opponent-tree")
def team_opponent_tree(team_id: int):
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    all_team_scrims = get_team_history_scrims(team)
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
    hero_pool_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
    hero_pool_scrims = filter_scrims_by_map_type(hero_pool_scrims, selected_map_type)
    return jsonify(build_opponent_tree_model(team_scrims, hero_pool_scrims=hero_pool_scrims))


@app.route("/teams/matchup-tree")
def team_matchup_tree():
    db = get_db()
    team_a_id = request.args.get("team_a", type=int)
    team_b_id = request.args.get("team_b", type=int)
    if not team_a_id or not team_b_id:
        return jsonify({"error": "Both team_a and team_b are required."}), 400

    team_rows = db.execute(
        "SELECT * FROM teams WHERE id IN (?, ?)",
        (team_a_id, team_b_id),
    ).fetchall()
    team_lookup = {row["id"]: row for row in team_rows}
    team_a = team_lookup.get(team_a_id)
    team_b = team_lookup.get(team_b_id)
    if team_a is None or team_b is None:
        abort(404)

    selected_map_type = get_selected_map_type(request.args.get("map_type", "all"))
    season_value = request.args.get("season", "")
    selected_map_name = (request.args.get("map", "") or "").strip()
    include_scrims = _bool_arg("include_scrims", True)
    include_tournaments = _bool_arg("include_tournaments", True)

    def filtered_scrims_for(team_row) -> tuple[list[dict], list[dict]]:
        all_team_scrims = get_team_history_for_sources(
            team_row,
            include_scrims=include_scrims,
            include_tournaments=include_tournaments,
        )
        season_options = get_scrim_season_options(all_team_scrims)
        default_season = get_current_season_from_recent_scrim(all_team_scrims)
        has_unseasoned_scrims = any(
            not normalize_season_value(scrim.get("season", ""))
            for scrim in all_team_scrims
        )
        # For the matchup model, default to "all" so the user's explicit
        # "All Seasons" selection is always respected.  Specific season
        # selections are still honoured normally.
        effective_default = "all" if not season_value or season_value.lower() == "all" else default_season
        selected_season = get_selected_season(
            season_value,
            season_options,
            allow_unspecified=has_unseasoned_scrims,
            default_season=effective_default,
            strict=True,
        )
        team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
        team_scrims = filter_scrims_by_map_type(team_scrims, selected_map_type)
        hero_pool_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
        hero_pool_scrims = filter_scrims_by_map_type(hero_pool_scrims, selected_map_type)
        if selected_map_name and selected_map_name.lower() != "all":
            filtered = []
            for scrim in team_scrims:
                scrim_copy = dict(scrim)
                scrim_copy["maps"] = [
                    m for m in scrim.get("maps", [])
                    if ((m.get("map") or m.get("map_name") or "").strip().lower() == selected_map_name.lower())
                ]
                if scrim_copy["maps"]:
                    filtered.append(scrim_copy)
            return filtered, hero_pool_scrims
        return team_scrims, hero_pool_scrims

    team_a_scrims, team_a_hero_pool_scrims = filtered_scrims_for(team_a)
    team_b_scrims, team_b_hero_pool_scrims = filtered_scrims_for(team_b)
    return jsonify(
        build_matchup_tree_model(
            team_a["name"],
            team_a_scrims,
            team_b["name"],
            team_b_scrims,
            team_a_hero_pool_scrims=team_a_hero_pool_scrims,
            team_b_hero_pool_scrims=team_b_hero_pool_scrims,
        )
    )


@app.route("/teams/<int:team_id>/heroes/<path:hero_name>")
def team_hero_detail(team_id: int, hero_name: str):
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    target_hero = (hero_name or "").strip()
    if not target_hero:
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

    hero_insights = build_team_hero_insights(team_scrims, target_hero)
    if not hero_insights["summary"]["maps_played"]:
        flash(f"No comp data found for {target_hero}.", "error")
        return redirect(url_for("team_detail", team_id=team_id, season=selected_season, map_type=selected_map_type) + "#comps")

    return render_template(
        "hero_detail.html",
        team=team,
        hero_insights=hero_insights,
        map_images=MAP_IMAGES,
        selected_season=selected_season,
        selected_map_type=selected_map_type,
        season_options=season_options,
        has_unseasoned_scrims=has_unseasoned_scrims,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
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

    player_stats = compute_player_stats(player["name"], team_scrims)
    breakdown = build_player_hero_map_breakdown(
        player["name"],
        team_scrims,
        team_slots=TEAM_SLOTS,
        canonical_draft_hero=_canonical_draft_hero,
        get_map_outcome_for_slot=get_map_outcome_for_slot,
        map_modes=MAP_MODES,
        get_map_image_url=get_map_image_url,
    )
    primary_hero_row = breakdown["hero_rows"][0] if breakdown["hero_rows"] else None
    recent_map_rows = build_player_recent_maps(player["name"], team_scrims, limit=20)
    swap_summary = build_player_submap_swap_summary(player["name"], team_scrims, limit=20)
    player_ban_impact = build_player_ban_impact(player["name"], team_scrims)

    best_map_row = max(breakdown["map_rows"], key=lambda row: (row["win_rate"], row["maps"]), default=None)
    worst_map_row = min(breakdown["map_rows"], key=lambda row: (row["win_rate"], -row["maps"]), default=None)
    player_insights = {
        "unique_heroes": len(breakdown["hero_rows"]),
        "primary_hero": primary_hero_row,
        "best_map": best_map_row,
        "worst_map": worst_map_row,
    }

    return render_template(
        "player_detail.html",
        team=team,
        player=player,
        player_stats=player_stats,
        player_hero_rows=breakdown["hero_rows"],
        player_map_rows=breakdown["map_rows"],
        player_recent_maps=recent_map_rows,
        player_swap_summary=swap_summary,
        player_ban_impact=player_ban_impact,
        player_insights=player_insights,
        selected_season=selected_season,
        selected_map_type=selected_map_type,
        map_type_options=MAP_TYPES,
        season_options=season_options,
        has_unseasoned_scrims=has_unseasoned_scrims,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
    )


@app.route("/players/compare")
def player_compare():
    db = get_db()
    migrate_enemy_teams_to_team_database(db)
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

    comparison_scrims: list[dict] = []
    for selected_player in (player_a, player_b):
        if not selected_player:
            continue
        comparison_scrims.extend(get_scrims_for_team(selected_player["team_id"], selected_player.get("team_name", "")))

    season_options = get_scrim_season_options(comparison_scrims)
    default_season = get_current_season_from_recent_scrim(comparison_scrims)
    has_unseasoned_scrims = any(
        not normalize_season_value(scrim.get("season", ""))
        for scrim in comparison_scrims
    )
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )

    def load_player_payload(player_row: dict | None) -> dict | None:
        if player_row is None:
            return None

        team_scrims = get_scrims_for_team(player_row["team_id"], player_row.get("team_name", ""))
        team_scrims = filter_scrims_by_season(team_scrims, selected_season)
        stats = compute_player_stats(player_row["name"], team_scrims)
        breakdown = build_player_hero_map_breakdown(
            player_row["name"],
            team_scrims,
            team_slots=TEAM_SLOTS,
            canonical_draft_hero=_canonical_draft_hero,
            get_map_outcome_for_slot=get_map_outcome_for_slot,
            map_modes=MAP_MODES,
            get_map_image_url=get_map_image_url,
        )
        primary_hero = breakdown["hero_rows"][0] if breakdown["hero_rows"] else None
        ban_impact = build_player_ban_impact(player_row["name"], team_scrims)
        recent_maps = build_player_recent_maps(player_row["name"], team_scrims, limit=10)
        return {
            "profile": player_row,
            "stats": stats,
            "primary_hero": primary_hero,
            "hero_rows": breakdown["hero_rows"][:8],
            "map_rows": breakdown["map_rows"][:8],
            "hero_rows_full": breakdown["hero_rows"],
            "map_rows_full": breakdown["map_rows"],
            "ban_impact": ban_impact,
            "recent_maps": recent_maps,
        }

    payload_a = load_player_payload(player_a)
    payload_b = load_player_payload(player_b)

    shared_heroes = []
    shared_maps = []
    hero_winrate_differences = []
    map_winrate_differences = []
    if payload_a and payload_b:
        hero_lookup_a = {row["hero"]: row for row in payload_a["hero_rows_full"]}
        hero_lookup_b = {row["hero"]: row for row in payload_b["hero_rows_full"]}
        for hero_name in sorted(set(hero_lookup_a) & set(hero_lookup_b)):
            shared_heroes.append(
                {
                    "hero": hero_name,
                    "player_a_maps": hero_lookup_a[hero_name]["maps"],
                    "player_b_maps": hero_lookup_b[hero_name]["maps"],
                }
            )
        shared_heroes.sort(key=lambda row: row["player_a_maps"] + row["player_b_maps"], reverse=True)

        map_lookup_a = {row["map_name"]: row for row in payload_a["map_rows_full"]}
        map_lookup_b = {row["map_name"]: row for row in payload_b["map_rows_full"]}
        for map_name in sorted(set(map_lookup_a) & set(map_lookup_b)):
            shared_maps.append(
                {
                    "map_name": map_name,
                    "player_a_maps": map_lookup_a[map_name]["maps"],
                    "player_b_maps": map_lookup_b[map_name]["maps"],
                }
            )
        shared_maps.sort(key=lambda row: row["player_a_maps"] + row["player_b_maps"], reverse=True)

        for hero_name in sorted(set(hero_lookup_a) | set(hero_lookup_b)):
            row_a = hero_lookup_a.get(hero_name)
            row_b = hero_lookup_b.get(hero_name)
            a_maps = int((row_a or {}).get("maps") or 0)
            b_maps = int((row_b or {}).get("maps") or 0)
            a_decided = int((row_a or {}).get("decided_maps") or 0)
            b_decided = int((row_b or {}).get("decided_maps") or 0)
            a_wr = float((row_a or {}).get("win_rate") or 0) if a_decided else None
            b_wr = float((row_b or {}).get("win_rate") or 0) if b_decided else None
            diff = round(a_wr - b_wr, 1) if a_wr is not None and b_wr is not None else None
            hero_winrate_differences.append(
                {
                    "hero": hero_name,
                    "player_a_maps": a_maps,
                    "player_b_maps": b_maps,
                    "player_a_decided_maps": a_decided,
                    "player_b_decided_maps": b_decided,
                    "player_a_win_rate": a_wr,
                    "player_b_win_rate": b_wr,
                    "win_rate_diff": diff,
                }
            )

        hero_winrate_differences.sort(
            key=lambda row: (
                row["win_rate_diff"] is not None,
                abs(row["win_rate_diff"] or 0),
                row["player_a_decided_maps"] + row["player_b_decided_maps"],
                row["player_a_maps"] + row["player_b_maps"],
            ),
            reverse=True,
        )

        for map_name in sorted(set(map_lookup_a) | set(map_lookup_b)):
            row_a = map_lookup_a.get(map_name)
            row_b = map_lookup_b.get(map_name)
            a_maps = int((row_a or {}).get("maps") or 0)
            b_maps = int((row_b or {}).get("maps") or 0)
            a_decided = int((row_a or {}).get("decided_maps") or 0)
            b_decided = int((row_b or {}).get("decided_maps") or 0)
            a_wr = float((row_a or {}).get("win_rate") or 0) if a_decided else None
            b_wr = float((row_b or {}).get("win_rate") or 0) if b_decided else None
            diff = round(a_wr - b_wr, 1) if a_wr is not None and b_wr is not None else None
            map_winrate_differences.append(
                {
                    "map_name": map_name,
                    "player_a_maps": a_maps,
                    "player_b_maps": b_maps,
                    "player_a_decided_maps": a_decided,
                    "player_b_decided_maps": b_decided,
                    "player_a_win_rate": a_wr,
                    "player_b_win_rate": b_wr,
                    "win_rate_diff": diff,
                }
            )

        map_winrate_differences.sort(
            key=lambda row: (
                row["win_rate_diff"] is not None,
                abs(row["win_rate_diff"] or 0),
                row["player_a_decided_maps"] + row["player_b_decided_maps"],
                row["player_a_maps"] + row["player_b_maps"],
            ),
            reverse=True,
        )

    return render_template(
        "player_compare.html",
        player_options=options,
        selected_player_a_id=player_a_id,
        selected_player_b_id=player_b_id,
        player_a=payload_a,
        player_b=payload_b,
        shared_heroes=shared_heroes[:10],
        shared_maps=shared_maps[:10],
        hero_winrate_differences=hero_winrate_differences[:20],
        map_winrate_differences=map_winrate_differences[:20],
        selected_season=selected_season,
        season_options=season_options,
        has_unseasoned_scrims=has_unseasoned_scrims,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
    )


