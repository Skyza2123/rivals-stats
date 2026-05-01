# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

@app.route("/scrims/new")
def new_scrim():
    return redirect(f"{url_for('scrims')}#create-scrim")


@app.route("/tournaments")
def tournaments():
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    season_options = get_scrim_season_options(TOURNAMENT_MATCHES)
    has_unseasoned_matches = any(not normalize_season_value(match.get("season", "")) for match in TOURNAMENT_MATCHES)
    default_season = get_current_season_from_recent_scrim(TOURNAMENT_MATCHES)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_matches,
        default_season=default_season,
    )
    selected_team_id = (request.args.get("team_id", "") or "").strip()

    filtered_matches = filter_scrims_by_season(TOURNAMENT_MATCHES, selected_season)
    if selected_team_id:
        filtered_matches = [match for match in filtered_matches if str(match.get("team_id") or "") == selected_team_id]
    filtered_matches.sort(key=lambda m: m.get("scrim_date") or "", reverse=True)

    all_tournaments_sorted = sorted(TOURNAMENT_MATCHES, key=lambda m: m.get("scrim_date") or "", reverse=True)

    return render_template(
        "tournaments.html",
        tournaments=filtered_matches,
        all_tournaments=all_tournaments_sorted,
        teams=teams,
        today=date.today().isoformat(),
        season_options=season_options,
        selected_season=selected_season,
        selected_team_id=selected_team_id,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
        has_unseasoned_tournaments=has_unseasoned_matches,
        total_tournament_count=len(TOURNAMENT_MATCHES),
    )


@app.route("/tournaments/new")
def new_tournament():
    return redirect(f"{url_for('tournaments')}#create-tournament")


@app.route("/tournaments/create", methods=["POST"])
def create_tournament():
    global NEXT_TOURNAMENT_ID

    team_id = parse_team_id(request.form.get("team_id", ""))
    team_name = get_team_name_by_id(team_id)
    team_slot = normalize_match_team_slot(request.form.get("team_slot", "team1"))
    if team_id is not None and not team_name:
        flash("Selected affiliated team could not be found.", "error")
        return redirect(f"{url_for('tournaments')}#create-tournament")

    tournament_name = request.form.get("tournament_name", "").strip()
    if not tournament_name:
        flash("Please enter a tournament name.", "error")
        return redirect(f"{url_for('tournaments')}#create-tournament")

    match_date = request.form.get("scrim_date", "").strip()
    season = normalize_season_value(request.form.get("season", ""))
    notes = request.form.get("notes", "").strip()
    if not season:
        flash("Please set a season for this tournament.", "error")
        return redirect(f"{url_for('tournaments')}#create-tournament")

    tournament_match = {
        "id": NEXT_TOURNAMENT_ID,
        "tournament_name": tournament_name,
        "scrim_date": match_date,
        "season": season,
        "team_id": team_id,
        "team_name": team_name,
        "team_slot": team_slot,
        "tournament_teams": [],
        "team1_enemy_id": None,
        "team1_name": "",
        "team1_players": [],
        "team2_enemy_id": None,
        "team2_name": "",
        "team2_players": [],
        "notes": notes,
        "maps": [],
        "matches": [],
    }

    normalize_tournament_record(tournament_match)
    TOURNAMENT_MATCHES.append(tournament_match)
    NEXT_TOURNAMENT_ID += 1
    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_match["id"]))


@app.route("/scrims/create", methods=["POST"])
def create_scrim():
    global NEXT_SCRIM_ID

    team1_id = parse_team_id(request.form.get("team1_id", ""))
    team2_id = parse_team_id(request.form.get("team2_id", ""))
    team1_name = get_team_name_by_id(team1_id)
    team2_name = get_team_name_by_id(team2_id)
    if not team1_name or not team2_name:
        flash("Please select both teams for this scrim.", "error")
        return redirect(f"{url_for('scrims')}#create-scrim")
    if team1_id == team2_id:
        flash("Scrim teams must be different.", "error")
        return redirect(f"{url_for('scrims')}#create-scrim")

    scrim_date = request.form.get("scrim_date", "").strip()
    season = normalize_season_value(request.form.get("season", ""))
    notes = request.form.get("notes", "").strip()

    if not season:
        flash("Please set a season for this scrim.", "error")
        return redirect(f"{url_for('scrims')}#create-scrim")

    scrim = {
        "id": NEXT_SCRIM_ID,
        "opponent": team2_name,
        "enemy_team": team2_name,
        "enemy_team_id": team2_id,
        "scrim_date": scrim_date,
        "season": season,
        "team_id": team1_id,
        "team_name": team1_name,
        "team_slot": "team1",
        "team1_id": team1_id,
        "team1_name": team1_name,
        "team2_id": team2_id,
        "team2_name": team2_name,
        "notes": notes,
        "maps": [],
    }

    SCRIMS.append(scrim)
    NEXT_SCRIM_ID += 1
    save_app_state()

    return redirect(url_for("scrim_detail", scrim_id=scrim["id"]))


@app.route("/debug/scrims/<int:scrim_id>/move-to-tournament", methods=["POST"])
def debug_move_scrim_to_tournament(scrim_id: int):
    scrim = get_scrim_or_404(scrim_id)
    tournament_id = parse_team_id(request.form.get("tournament_id", ""))
    if tournament_id is None:
        flash("Choose an existing tournament to move this scrim into.", "error")
        return redirect(url_for("scrim_detail", scrim_id=scrim_id))

    tournament_record = get_tournament_or_404(tournament_id)
    participant_one, participant_two = get_scrim_participants(scrim)
    participant_one_label, participant_two_label = get_scrim_participant_labels(scrim)

    def _team_players(team_id: int | None) -> list[str]:
        if team_id is None:
            return []
        rows = get_db().execute(
            "SELECT name FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
            (team_id,),
        ).fetchall()
        return [row["name"] for row in rows if (row["name"] or "").strip()]

    def _find_or_add_tournament_team(team_id: int | None, team_name: str, fallback_players: list[str]) -> dict:
        normalized_name = (team_name or "").strip()
        for tournament_team in tournament_record.get("tournament_teams", []):
            if team_id is not None and tournament_team.get("source_team_id") == team_id:
                return tournament_team
            if normalized_name and str(tournament_team.get("name", "")).strip().lower() == normalized_name.lower():
                if team_id is not None and not tournament_team.get("source_team_id"):
                    tournament_team["source_team_id"] = team_id
                return tournament_team

        new_team = {
            "id": next_tournament_team_id(tournament_record),
            "name": normalized_name or "Unknown Team",
            "players": _team_players(team_id) or fallback_players,
        }
        if team_id is not None:
            new_team["source_team_id"] = team_id
        tournament_record.setdefault("tournament_teams", []).append(new_team)
        return new_team

    team1 = _find_or_add_tournament_team(
        participant_one.get("id"),
        participant_one_label or "Team 1",
        scrim.get("team1_players", []),
    )
    team2 = _find_or_add_tournament_team(
        participant_two.get("id"),
        participant_two_label or "Team 2",
        scrim.get("team2_players", []),
    )

    moved_maps = copy.deepcopy(scrim.get("maps", []))
    for map_entry in moved_maps:
        original_team1_id = map_entry.get("team1_id")
        original_team2_id = map_entry.get("team2_id")
        map_team1 = team1
        map_team2 = team2
        if original_team1_id == participant_two.get("id") or original_team2_id == participant_one.get("id"):
            map_team1 = team2
            map_team2 = team1
        map_entry["team1_tournament_team_id"] = map_team1["id"]
        map_entry["team2_tournament_team_id"] = map_team2["id"]
        map_entry["team1_name"] = map_team1["name"]
        map_entry["team2_name"] = map_team2["name"]
        map_entry["our_team_slot"] = "team1" if map_team1["id"] == team1["id"] else "team2"

    tournament_match = {
        "id": next_tournament_match_id(tournament_record),
        "scrim_date": scrim.get("scrim_date", ""),
        "notes": scrim.get("notes", ""),
        "team1_tournament_team_id": team1["id"],
        "team2_tournament_team_id": team2["id"],
        "team1_name": team1["name"],
        "team2_name": team2["name"],
        "maps": moved_maps,
    }
    tournament_record.setdefault("matches", []).append(tournament_match)
    normalize_tournament_record(tournament_record)
    SCRIMS.remove(scrim)
    save_app_state(allow_scrim_removal=True)
    flash("Moved scrim into the selected tournament.", "success")
    return redirect(url_for("tournament_match_detail", tournament_id=tournament_record["id"], match_id=tournament_match["id"]))


@app.route("/scrims/<int:scrim_id>")
def scrim_detail(scrim_id: int):
    scrim = get_scrim_or_404(scrim_id)
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    participant_one_label, participant_two_label = get_scrim_participant_labels(scrim)
    participant_one, participant_two = get_scrim_participants(scrim)

    participant_one_id = participant_one.get("id")
    participant_two_id = participant_two.get("id")
    participant_one_name = (participant_one_label or "").strip().lower()
    participant_two_name = (participant_two_label or "").strip().lower()

    def _resolve_participant_slot(map_entry: dict, participant_id: int | None, participant_name: str, fallback_slot: str) -> str:
        if participant_id is not None:
            if map_entry.get("team1_id") == participant_id:
                return "team1"
            if map_entry.get("team2_id") == participant_id:
                return "team2"

        team1_name = (map_entry.get("team1_name") or "").strip().lower()
        team2_name = (map_entry.get("team2_name") or "").strip().lower()
        if participant_name:
            if team1_name == participant_name:
                return "team1"
            if team2_name == participant_name:
                return "team2"

        return fallback_slot

    team1_score = 0
    team2_score = 0
    for map_entry in scrim.get("maps", []):
        left_raw, right_raw = split_score_pair(map_entry.get("score", ""))
        participant_one_slot = _resolve_participant_slot(map_entry, participant_one_id, participant_one_name, "team1")
        participant_two_slot = "team2" if participant_one_slot == "team1" else "team1"

        participant_one_outcome = get_map_outcome_for_slot(map_entry, participant_one_slot)
        if participant_one_outcome == "Win":
            team1_score += 1
            map_entry["participant_winner_label"] = participant_one_label
        elif participant_one_outcome == "Loss":
            team2_score += 1
            map_entry["participant_winner_label"] = participant_two_label
        else:
            map_entry["participant_winner_label"] = "Tie"

        if participant_one_slot == "team1":
            map_entry["participant_one_score"] = left_raw.strip()
            map_entry["participant_two_score"] = right_raw.strip()
        else:
            map_entry["participant_one_score"] = right_raw.strip()
            map_entry["participant_two_score"] = left_raw.strip()

        map_entry["participant_one_slot"] = participant_one_slot
        map_entry["participant_two_slot"] = participant_two_slot

    winner_label = "Tie"
    if team1_score > team2_score:
        winner_label = participant_one_label
    elif team2_score > team1_score:
        winner_label = participant_two_label

    return render_template(
        "scrim_detail.html",
        scrim=scrim,
        maps=MAPS,
        map_types=MAP_TYPES,
        map_images=MAP_IMAGES,
        map_submaps=MAP_SUBMAPS,
        sides=SIDES,
        results=RESULTS,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        teams=teams,
        tournaments=TOURNAMENT_MATCHES,
        team1_score=team1_score,
        team2_score=team2_score,
        winner_label=winner_label,
        match_label="Scrim",
        match_list_endpoint="scrims",
        match_detail_endpoint="scrim_detail",
        match_edit_endpoint="edit_scrim",
        match_delete_endpoint="delete_scrim",
        add_map_endpoint="add_map",
        map_detail_endpoint="map_detail",
        delete_map_endpoint="delete_map",
        participant_one_label=participant_one_label,
        participant_two_label=participant_two_label,
        participant_one_id=participant_one.get("id"),
        participant_two_id=participant_two.get("id"),
        split_score_pair=split_score_pair,
        opponent_field_label="Enemy Team",
        show_team_selector=True,
        attack_defense_maps=sorted(ATTACK_DEFENSE_MAPS),
    )


@app.route("/tournaments/<int:tournament_id>")
def tournament_detail(tournament_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    selected_perspective = normalize_match_team_slot(tournament_record.get("team_slot", "team1"))

    # Sync each tournament team's roster from the DB on every page load
    roster_changed = False
    for t_team in tournament_record.get("tournament_teams", []):
        if sync_tournament_team_with_db(t_team):
            roster_changed = True
    if roster_changed:
        normalize_tournament_record(tournament_record)
        save_app_state()

    match_summaries = build_tournament_match_summaries(tournament_record)
    overview_analytics = build_tournament_overview_analytics(tournament_record)
    tournament_ban_analytics = build_scrim_analytics(build_tournament_match_scrims(tournament_record, selected_perspective))
    total_maps = sum(summary["maps"] for summary in match_summaries)
    completed_maps = sum(summary["completed_maps"] for summary in match_summaries)

    return render_template(
        "tournament_detail.html",
        tournament=tournament_record,
        teams=teams,
        match_summaries=match_summaries,
        overview_analytics=overview_analytics,
        tournament_ban_analytics=tournament_ban_analytics,
        selected_perspective=selected_perspective,
        total_maps=total_maps,
        map_images=MAP_IMAGES,
        completed_maps=completed_maps,
        today=date.today().isoformat(),
        hero_roles=HERO_ROLES,
    )


@app.route("/tournaments/<int:tournament_id>/matches/add", methods=["POST"])
def add_tournament_match(tournament_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    team1_tournament_team_id = parse_team_id(request.form.get("team1_tournament_team_id", ""))
    team2_tournament_team_id = parse_team_id(request.form.get("team2_tournament_team_id", ""))
    team1 = get_tournament_team_by_id(tournament_record, team1_tournament_team_id)
    team2 = get_tournament_team_by_id(tournament_record, team2_tournament_team_id)

    if team1 is None or team2 is None:
        flash("Select two tournament teams for the match.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))
    if team1_tournament_team_id == team2_tournament_team_id:
        flash("Match teams must be different.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    tournament_match = {
        "id": next_tournament_match_id(tournament_record),
        "scrim_date": request.form.get("scrim_date", tournament_record.get("scrim_date", "")).strip(),
        "notes": request.form.get("notes", "").strip(),
        "team1_tournament_team_id": team1_tournament_team_id,
        "team2_tournament_team_id": team2_tournament_team_id,
        "team1_name": team1["name"],
        "team2_name": team2["name"],
        "maps": [],
    }
    tournament_record.setdefault("matches", []).append(tournament_match)
    save_app_state()
    return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=tournament_match["id"]))


@app.route("/tournaments/<int:tournament_id>/matches/import-file", methods=["POST"])
def import_tournament_match_file(tournament_id: int):
    global NEXT_MAP_ID

    tournament_record = get_tournament_or_404(tournament_id)
    uploaded_file = request.files.get("match_file")
    if uploaded_file is None or not uploaded_file.filename:
        flash("Choose a tournament match text file to import.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    try:
        file_bytes = uploaded_file.read()
        try:
            file_text = file_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            file_text = file_bytes.decode("latin-1")
        parsed_match = parse_tournament_match_text_import(file_text)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))
    except Exception:
        flash("Could not import that match file. Check the text format and try again.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    team1 = find_or_add_tournament_team_from_import(
        tournament_record,
        parsed_match["team1_name"],
        parsed_match.get("players_by_team", {}).get(parsed_match["team1_name"], []),
    )
    team2 = find_or_add_tournament_team_from_import(
        tournament_record,
        parsed_match["team2_name"],
        parsed_match.get("players_by_team", {}).get(parsed_match["team2_name"], []),
    )

    maps = []
    for parsed_map in parsed_match.get("maps", []):
        map_entry = copy.deepcopy(parsed_map)
        map_team1 = find_or_add_tournament_team_from_import(
            tournament_record,
            map_entry.get("team1_name", ""),
            parsed_match.get("players_by_team", {}).get(map_entry.get("team1_name", ""), []),
        )
        map_team2 = find_or_add_tournament_team_from_import(
            tournament_record,
            map_entry.get("team2_name", ""),
            parsed_match.get("players_by_team", {}).get(map_entry.get("team2_name", ""), []),
        )
        map_entry["id"] = NEXT_MAP_ID
        NEXT_MAP_ID += 1
        map_entry["team1_tournament_team_id"] = map_team1["id"]
        map_entry["team2_tournament_team_id"] = map_team2["id"]
        map_entry["team1_name"] = map_team1["name"]
        map_entry["team2_name"] = map_team2["name"]
        map_entry["our_team_slot"] = "team1" if map_team1["id"] == team1["id"] else "team2"
        map_entry["result"] = infer_result_from_score_text(map_entry.get("score", ""), slot=map_entry["our_team_slot"])
        map_entry["picked_by_tournament_team_id"] = None
        map_entry["picked_by_name"] = ""
        maps.append(map_entry)

    if not maps:
        flash("The uploaded file did not contain any map data.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    for index, map_entry in enumerate(maps):
        if index == 0:
            continue
        picker_id = get_tournament_map_loser_team_id(maps[index - 1])
        picker = find_tournament_team_by_id(tournament_record.get("tournament_teams", []), picker_id)
        if picker is not None:
            map_entry["picked_by_tournament_team_id"] = picker["id"]
            map_entry["picked_by_name"] = picker.get("name", "")

    match_date = request.form.get("scrim_date", "").strip() or tournament_record.get("scrim_date", "")
    notes = parsed_match.get("notes", "")
    if parsed_match.get("match_score"):
        notes = f"{notes}\nMatch score: {parsed_match['match_score']}".strip()
    tournament_match = {
        "id": next_tournament_match_id(tournament_record),
        "scrim_date": match_date,
        "notes": notes,
        "team1_tournament_team_id": team1["id"],
        "team2_tournament_team_id": team2["id"],
        "team1_name": team1["name"],
        "team2_name": team2["name"],
        "maps": maps,
    }
    normalize_tournament_match_record(tournament_match, tournament_record.get("tournament_teams", []))
    tournament_record.setdefault("matches", []).append(tournament_match)
    normalize_tournament_record(tournament_record)
    save_app_state()
    flash(f"Imported {team1['name']} vs {team2['name']} with {len(maps)} maps.", "success")
    return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=tournament_match["id"]))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>")
def tournament_match_detail(tournament_id: int, match_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    team1_map_wins = 0
    team2_map_wins = 0
    team1_tournament_team_id = tournament_match.get("team1_tournament_team_id")
    team2_tournament_team_id = tournament_match.get("team2_tournament_team_id")
    for map_entry in tournament_match.get("maps", []):
        winner_tournament_team_id = None
        left_raw, right_raw = split_score_pair(map_entry.get("score", ""))
        if left_raw and right_raw:
            try:
                left_score = float(left_raw)
                right_score = float(right_raw)
            except ValueError:
                left_score = right_score = 0.0
            if left_score > right_score:
                winner_tournament_team_id = map_entry.get("team1_tournament_team_id")
            elif right_score > left_score:
                winner_tournament_team_id = map_entry.get("team2_tournament_team_id")

        if winner_tournament_team_id is None:
            map_result = str(map_entry.get("result", "")).strip()
            if map_result == "Win":
                winner_tournament_team_id = map_entry.get("team1_tournament_team_id")
            elif map_result == "Loss":
                winner_tournament_team_id = map_entry.get("team2_tournament_team_id")

        if winner_tournament_team_id == team1_tournament_team_id:
            team1_map_wins += 1
        elif winner_tournament_team_id == team2_tournament_team_id:
            team2_map_wins += 1

    winner_label = "Tie"
    if team1_map_wins > team2_map_wins:
        winner_label = tournament_match.get("team1_name") or "Team 1"
    elif team2_map_wins > team1_map_wins:
        winner_label = tournament_match.get("team2_name") or "Team 2"

    return render_template(
        "tournament_match_detail.html",
        tournament=tournament_record,
        match=tournament_match,
        team1_map_wins=team1_map_wins,
        team2_map_wins=team2_map_wins,
        winner_label=winner_label,
        maps=MAPS,
        map_images=MAP_IMAGES,
        map_submaps=MAP_SUBMAPS,
        sides=SIDES,
        results=RESULTS,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        split_score_pair=split_score_pair,
    )


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/edit", methods=["POST"])
def edit_tournament_match(tournament_id: int, match_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    team1_tournament_team_id = parse_team_id(request.form.get("team1_tournament_team_id", ""))
    team2_tournament_team_id = parse_team_id(request.form.get("team2_tournament_team_id", ""))
    team1 = get_tournament_team_by_id(tournament_record, team1_tournament_team_id)
    team2 = get_tournament_team_by_id(tournament_record, team2_tournament_team_id)

    if team1 is None or team2 is None:
        flash("Select two tournament teams for the match.", "error")
        return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))
    if team1_tournament_team_id == team2_tournament_team_id:
        flash("Match teams must be different.", "error")
        return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))

    tournament_match["scrim_date"] = request.form.get("scrim_date", tournament_match.get("scrim_date", "")).strip()
    tournament_match["notes"] = request.form.get("notes", tournament_match.get("notes", "")).strip()
    tournament_match["team1_tournament_team_id"] = team1_tournament_team_id
    tournament_match["team2_tournament_team_id"] = team2_tournament_team_id
    tournament_match["team1_name"] = team1["name"]
    tournament_match["team2_name"] = team2["name"]

    for map_entry in tournament_match.get("maps", []):
        map_entry["team1_tournament_team_id"] = team1_tournament_team_id
        map_entry["team2_tournament_team_id"] = team2_tournament_team_id
        map_entry["team1_name"] = team1["name"]
        map_entry["team2_name"] = team2["name"]
        if map_entry.get("picked_by_tournament_team_id") not in {team1_tournament_team_id, team2_tournament_team_id}:
            map_entry["picked_by_tournament_team_id"] = None
            map_entry["picked_by_name"] = ""

    save_app_state()
    return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/delete", methods=["POST"])
def delete_tournament_match(tournament_id: int, match_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    tournament_record.setdefault("matches", []).remove(tournament_match)
    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/add-map", methods=["POST"])
def add_tournament_match_map(tournament_id: int, match_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)

    map_entry = build_match_map_entry_from_form()
    side1_tournament_team_id = parse_team_id(request.form.get("map_team1_tournament_team_id", ""))
    valid_team_ids = {
        tournament_match.get("team1_tournament_team_id"),
        tournament_match.get("team2_tournament_team_id"),
    }
    if side1_tournament_team_id not in valid_team_ids:
        flash("Choose which match team is on side 1 for this map.", "error")
        return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))

    side2_tournament_team_id = next(team_id for team_id in valid_team_ids if team_id != side1_tournament_team_id)
    team1 = get_tournament_team_by_id(tournament_record, side1_tournament_team_id)
    team2 = get_tournament_team_by_id(tournament_record, side2_tournament_team_id)
    map_entry["team1_tournament_team_id"] = side1_tournament_team_id
    map_entry["team2_tournament_team_id"] = side2_tournament_team_id
    map_entry["team1_name"] = team1.get("name", "") if team1 is not None else ""
    map_entry["team2_name"] = team2.get("name", "") if team2 is not None else ""

    picked_by_tournament_team_id = parse_team_id(request.form.get("picked_by_tournament_team_id", ""))
    if picked_by_tournament_team_id is not None:
        if picked_by_tournament_team_id not in {
            tournament_match.get("team1_tournament_team_id"),
            tournament_match.get("team2_tournament_team_id"),
        }:
            flash("Map picker must be one of the two match teams.", "error")
            return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))
        picker = get_tournament_team_by_id(tournament_record, picked_by_tournament_team_id)
        map_entry["picked_by_tournament_team_id"] = picked_by_tournament_team_id
        map_entry["picked_by_name"] = picker.get("name", "") if picker is not None else ""
    else:
        map_entry["picked_by_tournament_team_id"] = None
        map_entry["picked_by_name"] = ""

    tournament_match.setdefault("maps", []).append(map_entry)
    save_app_state()
    return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))


@app.route("/scrims/<int:scrim_id>/edit", methods=["POST"])
def edit_scrim(scrim_id: int):
    scrim = get_scrim_or_404(scrim_id)
    team1_id = parse_team_id(request.form.get("team1_id", ""))
    team2_id = parse_team_id(request.form.get("team2_id", ""))
    team1_name = get_team_name_by_id(team1_id)
    team2_name = get_team_name_by_id(team2_id)
    if not team1_name or not team2_name:
        flash("Please select both teams for this scrim.", "error")
        return redirect(url_for("scrim_detail", scrim_id=scrim_id))
    if team1_id == team2_id:
        flash("Scrim teams must be different.", "error")
        return redirect(url_for("scrim_detail", scrim_id=scrim_id))

    season = normalize_season_value(request.form.get("season", scrim.get("season", "")))
    if not season:
        flash("Please set a season for this scrim.", "error")
        return redirect(url_for("scrim_detail", scrim_id=scrim_id))

    scrim["opponent"] = team2_name
    scrim["enemy_team"] = team2_name
    scrim["enemy_team_id"] = team2_id
    scrim["scrim_date"] = request.form.get("scrim_date", scrim["scrim_date"]).strip()
    scrim["season"] = season
    scrim["team_id"] = team1_id
    scrim["team_name"] = team1_name
    scrim["team_slot"] = "team1"
    scrim["team1_id"] = team1_id
    scrim["team1_name"] = team1_name
    scrim["team2_id"] = team2_id
    scrim["team2_name"] = team2_name
    scrim["notes"] = request.form.get("notes", scrim["notes"]).strip()
    save_app_state()
    return redirect(url_for("scrim_detail", scrim_id=scrim_id))


@app.route("/tournaments/<int:tournament_id>/edit", methods=["POST"])
def edit_tournament(tournament_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    tournament_name = request.form.get("tournament_name", tournament_match.get("tournament_name", "")).strip()
    team_slot = normalize_match_team_slot(request.form.get("team_slot", tournament_match.get("team_slot", "team1")))
    if not tournament_name:
        flash("Please enter a tournament name.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    season = normalize_season_value(request.form.get("season", tournament_match.get("season", "")))
    if not season:
        flash("Please set a season for this tournament.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    tournament_match["tournament_name"] = tournament_name
    tournament_match["scrim_date"] = request.form.get("scrim_date", tournament_match.get("scrim_date", "")).strip()
    tournament_match["season"] = season
    tournament_match["team_slot"] = team_slot
    tournament_match["notes"] = request.form.get("notes", tournament_match.get("notes", "")).strip()
    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_id))


@app.route("/tournaments/<int:tournament_id>/teams", methods=["POST"])
def update_tournament_teams(tournament_id: int):
    tournament_match = get_tournament_or_404(tournament_id)

    team1_name = request.form.get("team1_name", tournament_match.get("team1_name", "")).strip()
    team2_name = request.form.get("team2_name", tournament_match.get("team2_name", "")).strip()
    if team1_name and team2_name and team1_name.lower() == team2_name.lower():
        flash("Tournament teams must be different.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    team1_players = parse_name_list(request.form.get("team1_players", ""))
    team2_players = parse_name_list(request.form.get("team2_players", ""))

    tournament_match["team1_name"] = team1_name
    tournament_match["team2_name"] = team2_name
    tournament_match["team1_enemy_id"] = None
    tournament_match["team2_enemy_id"] = None
    tournament_match["team1_players"] = team1_players
    tournament_match["team2_players"] = team2_players

    upsert_team_and_players(team1_name, team1_players)
    upsert_team_and_players(team2_name, team2_players)

    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_id))


@app.route("/tournaments/<int:tournament_id>/teams/add", methods=["POST"])
def add_tournament_team(tournament_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    source_team_id = parse_team_id(request.form.get("source_team_id", ""))

    if source_team_id is not None:
        source_team = get_db().execute(
            "SELECT id, name FROM teams WHERE id = ?",
            (source_team_id,),
        ).fetchone()
        if source_team is None:
            flash("Selected database team could not be found.", "error")
            return redirect(url_for("tournament_detail", tournament_id=tournament_id))

        team_name = (source_team["name"] or "").strip()
        players = [
            row["name"]
            for row in get_db().execute(
                "SELECT name FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
                (source_team_id,),
            ).fetchall()
        ]
    else:
        team_name = request.form.get("team_name", "").strip()
        if not team_name:
            flash("Please enter a tournament team name.", "error")
            return redirect(url_for("tournament_detail", tournament_id=tournament_id))
        players = parse_name_list(request.form.get("players", ""))

    existing_names = {str(team.get("name", "")).strip().lower() for team in tournament_match.get("tournament_teams", [])}
    if team_name.lower() in existing_names:
        flash("That tournament team already exists.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    new_tournament_team = {
        "id": next_tournament_team_id(tournament_match),
        "name": team_name,
        "players": players,
    }
    if source_team_id is not None:
        new_tournament_team["source_team_id"] = source_team_id

    tournament_match.setdefault("tournament_teams", []).append(new_tournament_team)
    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_id))


@app.route("/tournaments/<int:tournament_id>/teams/<int:tournament_team_id>/delete", methods=["POST"])
def delete_tournament_team(tournament_id: int, tournament_team_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    target_team = get_tournament_team_by_id(tournament_match, tournament_team_id)
    if target_team is None:
        abort(404)

    linked_match = next(
        (
            match for match in tournament_match.get("matches", [])
            if match.get("team1_tournament_team_id") == tournament_team_id
            or match.get("team2_tournament_team_id") == tournament_team_id
        ),
        None,
    )
    if linked_match is not None:
        flash("Remove this team from its tournament matches before deleting it.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    tournament_match["tournament_teams"] = [
        team for team in tournament_match.get("tournament_teams", []) if team.get("id") != tournament_team_id
    ]
    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_id))


@app.route("/scrims/<int:scrim_id>/delete", methods=["POST"])
def delete_scrim(scrim_id: int):
    scrim = get_scrim_or_404(scrim_id)
    SCRIMS.remove(scrim)
    save_app_state(allow_scrim_removal=True)
    return redirect(url_for("scrims"))


@app.route("/tournaments/<int:tournament_id>/delete", methods=["POST"])
def delete_tournament(tournament_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    TOURNAMENT_MATCHES.remove(tournament_match)
    save_app_state()
    return redirect(url_for("tournaments"))


@app.route("/scrims/<int:scrim_id>/add-map", methods=["POST"])
def add_map(scrim_id: int):
    scrim = get_scrim_or_404(scrim_id)

    map_entry = build_match_map_entry_from_form()
    participant_one, participant_two = get_scrim_participants(scrim)
    valid_team_ids = {
        participant_one.get("id"),
        participant_two.get("id"),
    }
    side1_team_id = parse_team_id(request.form.get("map_team1_team_id", ""))

    if side1_team_id in valid_team_ids and participant_one.get("id") and participant_two.get("id"):
        if side1_team_id == participant_one.get("id"):
            side1_team = participant_one
            side2_team = participant_two
        else:
            side1_team = participant_two
            side2_team = participant_one
    else:
        side1_team = participant_one
        side2_team = participant_two

    map_entry["team1_id"] = side1_team.get("id")
    map_entry["team2_id"] = side2_team.get("id")
    map_entry["team1_name"] = side1_team.get("name", "")
    map_entry["team2_name"] = side2_team.get("name", "")
    map_entry["our_team_slot"] = "team1" if side1_team.get("id") == participant_one.get("id") else "team2"
    inferred_result = infer_result_from_score_text(map_entry.get("score", ""), slot=map_entry["our_team_slot"])
    if inferred_result:
        map_entry["result"] = inferred_result

    scrim["maps"].append(map_entry)
    save_app_state()

    return redirect(url_for("scrim_detail", scrim_id=scrim_id))


@app.route("/tournaments/<int:tournament_id>/add-map", methods=["POST"])
def add_tournament_map(tournament_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    team1_tournament_team_id = parse_team_id(request.form.get("team1_tournament_team_id", ""))
    team2_tournament_team_id = parse_team_id(request.form.get("team2_tournament_team_id", ""))
    team1 = get_tournament_team_by_id(tournament_match, team1_tournament_team_id)
    team2 = get_tournament_team_by_id(tournament_match, team2_tournament_team_id)
    if team1 is None or team2 is None:
        flash("Select two tournament teams before adding a map.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))
    if team1_tournament_team_id == team2_tournament_team_id:
        flash("Map teams must be different.", "error")
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))

    map_entry = build_match_map_entry_from_form()
    map_entry["team1_tournament_team_id"] = team1_tournament_team_id
    map_entry["team2_tournament_team_id"] = team2_tournament_team_id
    map_entry["team1_name"] = team1["name"]
    map_entry["team2_name"] = team2["name"]
    map_entry["our_team_slot"] = "team1"
    inferred_result = infer_result_from_score_text(map_entry.get("score", ""), slot=map_entry["our_team_slot"])
    if inferred_result:
        map_entry["result"] = inferred_result
    tournament_match["maps"].append(map_entry)
    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>")
def map_detail(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)

    context = build_match_map_detail_context(scrim, map_entry, is_tournament=False)

    return render_template(
        "map_detail.html",
        scrim=scrim,
        is_tournament=False,
        back_to_detail_endpoint="scrim_detail",
        match_detail_endpoint="map_detail",
        delete_map_endpoint="delete_map",
        update_draft_endpoint="update_draft",
        update_notes_endpoint="update_notes",
        update_vod_endpoint="update_vod",
        update_map_info_endpoint="update_map_info",
        update_comp_endpoint="update_comp",
        update_comp_section_endpoint="update_comp_section",
        add_comp_section_endpoint="add_comp_section",
        delete_event_endpoint="delete_event",
        add_event_endpoint="add_event_to_map",
        detail_parent_id=scrim_id,
        detail_match_id=scrim_id,
        **context,
    )


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>")
def tournament_match_map_detail(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)

    context = build_match_map_detail_context(
        tournament_match,
        map_entry,
        is_tournament=True,
        tournament_record=tournament_record,
    )

    return render_template(
        "map_detail.html",
        scrim=tournament_match,
        tournament=tournament_record,
        is_tournament=True,
        back_to_detail_endpoint="tournament_match_detail",
        match_detail_endpoint="tournament_match_map_detail",
        delete_map_endpoint="delete_tournament_match_map",
        update_draft_endpoint="update_tournament_match_draft",
        update_notes_endpoint="update_tournament_match_notes",
        update_vod_endpoint="update_tournament_match_vod",
        update_map_info_endpoint="update_tournament_match_map_info",
        update_comp_endpoint="update_tournament_match_comp",
        update_comp_section_endpoint="update_tournament_match_comp_section",
        add_comp_section_endpoint="add_tournament_match_comp_section",
        delete_event_endpoint="delete_tournament_match_event",
        add_event_endpoint="add_tournament_match_event_to_map",
        detail_parent_id=tournament_id,
        detail_match_id=match_id,
        **context,
    )


@app.route("/scrims/<int:scrim_id>/timelines")
def scrim_timelines(scrim_id: int):
    return redirect(url_for("scrim_detail", scrim_id=scrim_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/timelines")
def tournament_match_timelines(tournament_id: int, match_id: int):
    return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))


