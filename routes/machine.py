# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

@app.route("/draft-simulator")
def draft_simulator():
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    return render_template(
        "draft_simulator.html",
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        teams=teams,
    )


@app.route("/draft-reasoner")
def draft_reasoner():
    # Keep legacy URL working, but route everyone into the unified Machine UI.
    return redirect(url_for("machine", tab="reasoner"))


@app.route("/machine")
def machine():
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    machine_history = SCRIMS + TOURNAMENT_MATCHES
    season_options = get_scrim_season_options(machine_history)
    default_season = get_current_season_from_recent_scrim(machine_history)
    has_unseasoned = any(not normalize_season_value(s.get("season", "")) for s in machine_history)
    return render_template(
        "machine.html",
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        teams=teams,
        maps=MAPS,
        season_options=season_options,
        default_season=default_season,
        has_unseasoned=has_unseasoned,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
    )


@app.route("/api/draft-reasoner/enemy-scouting")
def api_draft_reasoner_enemy_scouting():
    """Return tournament loss analysis for the enemy team.

    For each map where team_b lost to another team, aggregate:
    - bans the winning team used against team_b
    - heroes the winning team played on those maps
    - heroes team_b themselves played on those losing maps (shows what didn't work)
    Only includes season-filtered data when season is specified.
    """
    db = get_db()
    team_b_id = request.args.get("team_b", type=int)
    season_value = (request.args.get("season", "") or "").strip()
    if not team_b_id:
        return jsonify({"error": "team_b is required"}), 400

    team_b_row = db.execute("SELECT * FROM teams WHERE id = ?", (team_b_id,)).fetchone()
    if team_b_row is None:
        abort(404)

    team_id = int(team_b_row["id"])
    team_name = (team_b_row["name"] or "").strip().lower()

    # Counts indexed by hero name
    ban_counts: defaultdict[str, int] = defaultdict(int)   # bans by winning teams vs team_b
    winner_hero_counts: defaultdict[str, int] = defaultdict(int)  # heroes winning teams played
    loser_hero_counts: defaultdict[str, int] = defaultdict(int)   # heroes team_b played when losing
    loss_map_total = 0
    match_summaries: list[dict] = []

    for tournament_record in TOURNAMENT_MATCHES:
        # Season filter
        rec_season = normalize_season_value(tournament_record.get("season", ""))
        if season_value and season_value.lower() not in ("all", "") and rec_season:
            if season_value != rec_season:
                continue

        # Find team_b's entry in this tournament
        team_b_entry: dict | None = None
        for t_team in tournament_record.get("tournament_teams", []):
            if not isinstance(t_team, dict):
                continue
            src_id = t_team.get("source_team_id")
            t_name = (t_team.get("name") or "").strip().lower()
            if (isinstance(src_id, int) and src_id == team_id) or (not src_id and t_name and t_name == team_name):
                team_b_entry = t_team
                break

        if team_b_entry is None:
            continue

        b_t_id = team_b_entry.get("id")

        for match in tournament_record.get("matches", []):
            if not isinstance(match, dict):
                continue

            # Determine which slot team_b occupies
            if match.get("team1_tournament_team_id") == b_t_id:
                b_slot, w_slot = "team1", "team2"
                opponent_name = (match.get("team2_name") or "").strip() or "Opponent"
            elif match.get("team2_tournament_team_id") == b_t_id:
                b_slot, w_slot = "team2", "team1"
                opponent_name = (match.get("team1_name") or "").strip() or "Opponent"
            else:
                continue

            map_losses: list[str] = []
            for map_entry in match.get("maps", []):
                if not isinstance(map_entry, dict):
                    continue

                outcome = get_map_outcome_for_slot(map_entry, b_slot)
                if outcome != "Loss":
                    continue

                loss_map_total += 1
                map_name = (map_entry.get("map_name") or map_entry.get("map") or "").strip()
                if map_name:
                    map_losses.append(map_name)

                # Winning team's bans
                draft_data = map_entry.get("draft", {})
                if isinstance(draft_data, dict):
                    w_draft = draft_data.get(w_slot, {})
                    if isinstance(w_draft, dict):
                        for slot_key, hero_val in w_draft.items():
                            if slot_key.startswith("ban") and hero_val:
                                h = _canonical_draft_hero(hero_val)
                                if h:
                                    ban_counts[h] += 1

                # Winning team's heroes played
                for section in map_entry.get("comp", []):
                    if not isinstance(section, dict):
                        continue
                    for slot in section.get(w_slot, []):
                        if isinstance(slot, dict):
                            h = _canonical_draft_hero(slot.get("hero", ""))
                            if h:
                                winner_hero_counts[h] += 1
                    # Team_b heroes on losing maps
                    for slot in section.get(b_slot, []):
                        if isinstance(slot, dict):
                            h = _canonical_draft_hero(slot.get("hero", ""))
                            if h:
                                loser_hero_counts[h] += 1

            if map_losses:
                match_summaries.append({
                    "opponent": opponent_name,
                    "maps_lost": map_losses,
                    "count": len(map_losses),
                })

    match_summaries.sort(key=lambda r: r["count"], reverse=True)

    def _to_rows(counts: dict[str, int], total_maps: int) -> list[dict]:
        rows = [
            {
                "hero": h,
                "count": c,
                "rate": round((c / total_maps) * 100, 1) if total_maps else 0.0,
            }
            for h, c in counts.items()
        ]
        rows.sort(key=lambda r: r["count"], reverse=True)
        return rows[:15]

    return jsonify({
        "team_b_name": (team_b_row["name"] or "").strip(),
        "loss_maps_total": loss_map_total,
        "winning_team_bans": _to_rows(ban_counts, loss_map_total),
        "winning_team_heroes": _to_rows(winner_hero_counts, loss_map_total),
        "enemy_heroes_when_losing": _to_rows(loser_hero_counts, loss_map_total),
        "match_summaries": match_summaries[:10],
    })


@app.route("/api/draft-reasoner/model")
def api_draft_reasoner_model():
    """Return the full matchup model plus per-team comfort/ban/comp data for the reasoner."""
    db = get_db()
    team_a_id = request.args.get("team_a", type=int)
    team_b_id = request.args.get("team_b", type=int)
    if not team_a_id or not team_b_id:
        return jsonify({"error": "Both team_a and team_b are required."}), 400

    team_rows = db.execute(
        "SELECT * FROM teams WHERE id IN (?, ?)", (team_a_id, team_b_id)
    ).fetchall()
    team_lookup = {row["id"]: row for row in team_rows}
    team_a = team_lookup.get(team_a_id)
    team_b = team_lookup.get(team_b_id)
    if team_a is None or team_b is None:
        abort(404)

    season_value = request.args.get("season", "")
    selected_map_type = get_selected_map_type(request.args.get("map_type", "all"))
    selected_map_name = (request.args.get("map", "") or "").strip()
    include_scrims = _bool_arg("include_scrims", True)
    include_tournaments = _bool_arg("include_tournaments", True)

    def _get_filtered_scrims(team_row) -> tuple[list[dict], list[dict]]:
        all_scrims = get_team_history_for_sources(
            team_row,
            include_scrims=include_scrims,
            include_tournaments=include_tournaments,
        )
        season_options = get_scrim_season_options(all_scrims)
        default_season = get_current_season_from_recent_scrim(all_scrims)
        has_unseasoned = any(not normalize_season_value(s.get("season", "")) for s in all_scrims)
        effective_default = "all" if not season_value or season_value.lower() == "all" else default_season
        selected_season = get_selected_season(
            season_value,
            season_options,
            allow_unspecified=has_unseasoned,
            default_season=effective_default,
            strict=True,
        )
        scrims = filter_scrims_by_season(all_scrims, selected_season)
        scrims = filter_scrims_by_map_type(scrims, selected_map_type)
        hero_pool_scrims = filter_scrims_by_season(all_scrims, selected_season)
        hero_pool_scrims = filter_scrims_by_map_type(hero_pool_scrims, selected_map_type)
        if selected_map_name and selected_map_name.lower() != "all":
            filtered = []
            for scrim in scrims:
                scrim_copy = dict(scrim)
                scrim_copy["maps"] = [
                    m for m in scrim.get("maps", [])
                    if ((m.get("map") or m.get("map_name") or "").strip().lower() == selected_map_name.lower())
                ]
                if scrim_copy["maps"]:
                    filtered.append(scrim_copy)
            return filtered, hero_pool_scrims
        return scrims, hero_pool_scrims

    def _get_team_roster(team_id: int) -> list[dict]:
        rows = db.execute(
            """
            SELECT name, role, main_hero, COALESCE(is_sub, 0) AS is_sub
            FROM players
            WHERE team_id = ?
            ORDER BY COALESCE(is_sub, 0), name COLLATE NOCASE
            """,
            (team_id,),
        ).fetchall()
        return [
            {
                "name": (row["name"] or "").strip(),
                "role": (row["role"] or "").strip(),
                "main_hero": normalize_hero_slot_value(row["main_hero"] or ""),
                "is_sub": bool(row["is_sub"]),
            }
            for row in rows
            if (row["name"] or "").strip()
        ]

    a_scrims, a_hero_pool_scrims = _get_filtered_scrims(team_a)
    b_scrims, b_hero_pool_scrims = _get_filtered_scrims(team_b)
    matchup = build_matchup_tree_model(
        team_a["name"],
        a_scrims,
        team_b["name"],
        b_scrims,
        team_a_hero_pool_scrims=a_hero_pool_scrims,
        team_b_hero_pool_scrims=b_hero_pool_scrims,
    )
    teams_payload = matchup.get("teams", [])

    def _build_player_hero_rows(roster: list[dict], hero_pool_scrims: list[dict]) -> list[dict]:
        rows = []
        for player in roster:
            player_name = (player.get("name") or "").strip()
            if not player_name:
                continue
            bd = build_player_hero_map_breakdown(
                player_name,
                hero_pool_scrims,
                team_slots=TEAM_SLOTS,
                canonical_draft_hero=_canonical_draft_hero,
                get_map_outcome_for_slot=get_map_outcome_for_slot,
                map_modes=MAP_MODES,
                get_map_image_url=get_map_image_url,
            )
            hero_rows = bd.get("hero_rows", [])
            total_maps = round(sum(h.get("maps", 0) for h in hero_rows), 2)
            top_heroes = [
                {
                    "hero": h["hero"],
                    "maps": h["maps"],
                    "appearances": h["maps"],
                    "usage_rate": round((h["maps"] / total_maps) * 100, 1) if total_maps else 0.0,
                    "win_rate": h.get("win_rate", 0),
                }
                for h in hero_rows
            ]
            rows.append({
                "player": player_name,
                "maps": total_maps,
                "appearances": total_maps,
                "overall_win_rate": 0.0,
                "top_heroes": top_heroes,
            })
        rows.sort(key=lambda r: (r["maps"], r["player"].lower()), reverse=True)
        return rows

    if len(teams_payload) >= 1:
        roster_a = _get_team_roster(team_a_id)
        teams_payload[0]["roster_players"] = roster_a
        teams_payload[0].setdefault("model", {})["player_hero_rows"] = _build_player_hero_rows(roster_a, a_hero_pool_scrims)
    if len(teams_payload) >= 2:
        roster_b = _get_team_roster(team_b_id)
        teams_payload[1]["roster_players"] = roster_b
        teams_payload[1].setdefault("model", {})["player_hero_rows"] = _build_player_hero_rows(roster_b, b_hero_pool_scrims)
    return jsonify(matchup)


