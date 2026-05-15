# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/delete", methods=["POST"])
def delete_tournament_match_map(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    tournament_match["maps"].remove(map_entry)
    save_app_state()
    return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/update-draft", methods=["POST"])
def update_tournament_match_draft(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)

    map_entry["draft"] = {
        "team1": {
            "ban1": request.form.get("team1_ban1", "").strip(),
            "protect1": request.form.get("team1_protect1", "").strip(),
            "ban2": request.form.get("team1_ban2", "").strip(),
            "ban3": request.form.get("team1_ban3", "").strip(),
            "ban4": request.form.get("team1_ban4", "").strip(),
            "protect2": request.form.get("team1_protect2", "").strip(),
        },
        "team2": {
            "ban1": request.form.get("team2_ban1", "").strip(),
            "ban2": request.form.get("team2_ban2", "").strip(),
            "protect1": request.form.get("team2_protect1", "").strip(),
            "ban3": request.form.get("team2_ban3", "").strip(),
            "protect2": request.form.get("team2_protect2", "").strip(),
            "ban4": request.form.get("team2_ban4", "").strip(),
        },
    }
    save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/update-notes", methods=["POST"])
def update_tournament_match_notes(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    map_entry["notes"] = request.form.get("notes", "").strip()
    save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/update-vod", methods=["POST"])
def update_tournament_match_vod(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    map_entry["vod_url"] = request.form.get("vod_url", "").strip()
    save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/update-info", methods=["POST"])
def update_tournament_match_map_info(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    update_map_type_from_form(map_entry)
    
    # Get score and auto-calculate result if not provided
    score_team1 = request.form.get("score_team1", "").strip()
    score_team2 = request.form.get("score_team2", "").strip()
    score = request.form.get("score", "").strip()
    
    # Prefer team-specific scores if provided
    if score_team1 or score_team2:
        map_entry["score"] = f"{score_team1}-{score_team2}".strip("-")
    else:
        map_entry["score"] = score
    
    # Manual result selection (no auto-calculation from score)
    map_entry["result"] = normalize_result_value(request.form.get("result", ""))
    if not map_entry["result"] and map_entry.get("score"):
        inferred = infer_result_from_score_text(
            map_entry.get("score", ""),
            slot=map_entry.get("our_team_slot", "team1"),
        )
        if inferred in RESULTS:
            map_entry["result"] = inferred

    raw_map_team1_id = request.form.get("map_team1_tournament_team_id", "")
    if raw_map_team1_id:
        side1_tournament_team_id = parse_team_id(raw_map_team1_id)
        valid_team_ids = {
            tournament_match.get("team1_tournament_team_id"),
            tournament_match.get("team2_tournament_team_id"),
        }
        if side1_tournament_team_id not in valid_team_ids:
            flash("Choose a valid match team for side 1.", "error")
            return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))
        remaining_team_ids = [team_id for team_id in valid_team_ids if team_id is not None and team_id != side1_tournament_team_id]
        if not remaining_team_ids:
            flash("Match teams are not configured correctly. Please set two different teams on the match.", "error")
            return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))
        side2_tournament_team_id = remaining_team_ids[0]
        team1 = get_tournament_team_by_id(tournament_record, side1_tournament_team_id)
        team2 = get_tournament_team_by_id(tournament_record, side2_tournament_team_id)
        map_entry["team1_tournament_team_id"] = side1_tournament_team_id
        map_entry["team2_tournament_team_id"] = side2_tournament_team_id
        map_entry["team1_name"] = team1.get("name", "") if team1 is not None else ""
        map_entry["team2_name"] = team2.get("name", "") if team2 is not None else ""
        map_entry["our_team_slot"] = "team1" if side1_tournament_team_id == tournament_match.get("team1_tournament_team_id") else "team2"
        inferred_result = infer_result_from_score_text(map_entry.get("score", ""), slot=map_entry["our_team_slot"])
        if inferred_result:
            map_entry["result"] = inferred_result

    picked_by_tournament_team_id = parse_team_id(request.form.get("picked_by_tournament_team_id", ""))
    if picked_by_tournament_team_id is not None:
        if picked_by_tournament_team_id not in {
            tournament_match.get("team1_tournament_team_id"),
            tournament_match.get("team2_tournament_team_id"),
        }:
            flash("Map picker must be one of the two match teams.", "error")
            return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))
        picker = get_tournament_team_by_id(tournament_record, picked_by_tournament_team_id)
        map_entry["picked_by_tournament_team_id"] = picked_by_tournament_team_id
        map_entry["picked_by_name"] = picker.get("name", "") if picker is not None else ""
    else:
        map_entry["picked_by_tournament_team_id"] = None
        map_entry["picked_by_name"] = ""

    has_submaps = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), []))
    if has_submaps:
        map_entry["side"] = request.form.get("side", map_entry.get("side", "")).strip()
    else:
        map_entry["side"] = ""
    save_app_state()
    if request.form.get("next") == "match":
        return redirect(url_for("tournament_match_detail", tournament_id=tournament_id, match_id=match_id))
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/update-comp", methods=["POST"])
def update_tournament_match_comp(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    use_section_sides = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), [])) or map_entry.get("map_name") in ATTACK_DEFENSE_MAPS
    section_count = int(request.form.get("section_count", "1"))
    sections = []
    for s in range(section_count):
        side_value = request.form.get(f"sec_{s}_side", "").strip() if use_section_sides else ""
        if side_value not in SIDES:
            side_value = ""
        sec = {
            "submap": request.form.get(f"sec_{s}_submap", "").strip(),
            "side": side_value,
            "score": request.form.get(f"sec_{s}_score", "").strip(),
            "team1": [],
            "team2": [],
        }
        for i in range(6):
            sec["team1"].append({
                "hero": request.form.get(f"sec_{s}_team1_hero_{i}", "").strip(),
                "player": request.form.get(f"sec_{s}_team1_player_{i}", "").strip(),
            })
            sec["team2"].append({
                "hero": request.form.get(f"sec_{s}_team2_hero_{i}", "").strip(),
                "player": request.form.get(f"sec_{s}_team2_player_{i}", "").strip(),
            })
        auto_assign_section_players_from_heroes(
            tournament_match,
            map_entry,
            sec,
            is_tournament=True,
            tournament_record=tournament_record,
        )
        sections.append(sec)
    map_entry["comp"] = sections
    save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/update-comp-section/<int:section_index>", methods=["POST"])
def update_tournament_match_comp_section(tournament_id: int, match_id: int, map_id: int, section_index: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    while len(map_entry.get("comp", [])) <= section_index:
        map_entry.setdefault("comp", []).append(build_default_comp_sections(map_entry.get("map_name", ""))[0])

    section = map_entry["comp"][section_index]
    score_team1 = request.form.get("score_team1", "").strip()
    score_team2 = request.form.get("score_team2", "").strip()
    if score_team1 or score_team2:
        section["score"] = f"{score_team1}-{score_team2}".strip("-")
    else:
        section["score"] = ""
    side_value = request.form.get("side", section.get("side", "")).strip()
    if side_value not in SIDES:
        side_value = ""
    section["side"] = side_value
    
    # Store section result (submap/round), or infer from score text.
    section_result = normalize_result_value(request.form.get("section_result", ""))
    if section_result in RESULTS:
        section["result"] = section_result
    else:
        inferred_submap_result = infer_result_from_score_text(section.get("score", ""), slot="team1")
        if inferred_submap_result in RESULTS:
            section["result"] = inferred_submap_result
        else:
            section.pop("result", None)

    for i in range(6):
        section["team1"][i]["hero"] = request.form.get(f"team1_hero_{i}", "").strip()
        section["team1"][i]["player"] = request.form.get(f"team1_player_{i}", "").strip()
        section["team2"][i]["hero"] = request.form.get(f"team2_hero_{i}", "").strip()
        section["team2"][i]["player"] = request.form.get(f"team2_player_{i}", "").strip()

    auto_assign_section_players_from_heroes(
        tournament_match,
        map_entry,
        section,
        is_tournament=True,
        tournament_record=tournament_record,
    )
    save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/delete-event/<int:event_id>", methods=["POST"])
def delete_tournament_match_event(tournament_id: int, match_id: int, map_id: int, event_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    event_to_delete = next((event for event in map_entry.get("events", []) if event.get("id") == event_id), None)
    if event_to_delete is None:
        abort(404)
    map_entry["events"].remove(event_to_delete)
    save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/add-event", methods=["POST"])
def add_tournament_match_event_to_map(tournament_id: int, match_id: int, map_id: int):
    global NEXT_EVENT_ID

    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    event_type = request.form.get("event_type", "").strip()
    if event_type not in EVENT_TYPES:
        flash("Please select a valid event type.", "error")
        return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))

    map_entry.setdefault("events", []).append(
        {
            "id": NEXT_EVENT_ID,
            "timestamp": request.form.get("timestamp", "").strip(),
            "event_type": event_type,
            "description": request.form.get("description", "").strip(),
        }
    )
    NEXT_EVENT_ID += 1
    save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>")
def tournament_map_detail(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)

    context = build_match_map_detail_context(tournament_match, map_entry, is_tournament=True)

    return render_template(
        "map_detail.html",
        scrim=tournament_match,
        is_tournament=True,
        back_to_detail_endpoint="tournament_detail",
        match_detail_endpoint="tournament_map_detail",
        delete_map_endpoint="delete_tournament_map",
        update_draft_endpoint="update_tournament_draft",
        update_notes_endpoint="update_tournament_notes",
        update_vod_endpoint="update_tournament_vod",
        update_map_info_endpoint="update_tournament_map_info",
        update_comp_endpoint="update_tournament_comp",
        update_comp_section_endpoint="update_tournament_comp_section",
        add_comp_section_endpoint="add_tournament_comp_section",
        delete_event_endpoint="delete_tournament_event",
        add_event_endpoint="add_tournament_event_to_map",
        detail_parent_id=tournament_id,
        detail_match_id=tournament_id,
        **context,
    )

    if map_entry.get("our_team_slot") not in TEAM_SLOTS:
        map_entry["our_team_slot"] = "team1"

    # Migrate old dict-style comp to section list
    if "comp" not in map_entry or isinstance(map_entry["comp"], dict):
        old = map_entry.get("comp", {})
        if isinstance(old, dict) and "team1" in old:
            map_entry["comp"] = [{
                "submap": "",
                "side": map_entry.get("side", ""),
                "team1": old["team1"],
                "team2": old["team2"],
            }]
        else:
            map_entry["comp"] = build_default_comp_sections(map_entry["map_name"])

    use_section_sides = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), [])) or map_entry.get("map_name") in ATTACK_DEFENSE_MAPS
    for sec in map_entry.get("comp", []):
        sec.setdefault("submap", "")
        sec.setdefault("score", "")
        side_value = (sec.get("side", "") or "").strip()
        if not use_section_sides:
            side_value = ""
        elif side_value not in SIDES:
            side_value = ""
        sec["side"] = side_value

    # Get team roster for player selection
    db = get_db()
    team_id = scrim.get("team_id")
    team_players = []
    staff_roles = {"Coach", "AC", "Analyst"}
    if team_id:
        player_rows = db.execute(
            "SELECT name, role FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
            (team_id,),
        ).fetchall()
        team_players = [
            row["name"]
            for row in player_rows
            if (row["name"] or "").strip() and (row["role"] or "").strip() not in staff_roles
        ]

    # Get enemy team info and players if available
    enemy_team_data = None
    enemy_players = []
    enemy_team_id = scrim.get("enemy_team_id")
    if enemy_team_id:
        enemy_team_rows = db.execute(
            "SELECT id, name, notes FROM enemy_teams WHERE id = ?",
            (enemy_team_id,),
        ).fetchone()
        if enemy_team_rows:
            enemy_team_data = dict(enemy_team_rows)
            enemy_player_rows = db.execute(
                "SELECT name, role, main_hero FROM enemy_players WHERE enemy_team_id = ? ORDER BY name COLLATE NOCASE",
                (enemy_team_id,),
            ).fetchall()
            enemy_players = [dict(row) for row in enemy_player_rows]

    return render_template(
        "map_detail.html",
        scrim=scrim,
        map_entry=map_entry,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        map_images=MAP_IMAGES,
        map_submaps=MAP_SUBMAPS,
        maps=MAPS,
        sides=SIDES,
        results=RESULTS,
        event_types=EVENT_TYPES,
        team_players=team_players,
        enemy_team=enemy_team_data,
        enemy_players=enemy_players,
    )


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/delete", methods=["POST"])
def delete_map(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    scrim["maps"].remove(map_entry)
    save_app_state()
    return redirect(url_for("scrim_detail", scrim_id=scrim_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/delete", methods=["POST"])
def delete_tournament_map(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    tournament_match["maps"].remove(map_entry)
    save_app_state()
    return redirect(url_for("tournament_detail", tournament_id=tournament_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-draft", methods=["POST"])
def update_draft(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)

    map_entry["draft"] = {
        "team1": {
            "ban1": request.form.get("team1_ban1", "").strip(),
            "protect1": request.form.get("team1_protect1", "").strip(),
            "ban2": request.form.get("team1_ban2", "").strip(),
            "ban3": request.form.get("team1_ban3", "").strip(),
            "ban4": request.form.get("team1_ban4", "").strip(),
            "protect2": request.form.get("team1_protect2", "").strip(),
        },
        "team2": {
            "ban1": request.form.get("team2_ban1", "").strip(),
            "ban2": request.form.get("team2_ban2", "").strip(),
            "protect1": request.form.get("team2_protect1", "").strip(),
            "ban3": request.form.get("team2_ban3", "").strip(),
            "protect2": request.form.get("team2_protect2", "").strip(),
            "ban4": request.form.get("team2_ban4", "").strip(),
        },
    }
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/update-draft", methods=["POST"])
def update_tournament_draft(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)

    map_entry["draft"] = {
        "team1": {
            "ban1": request.form.get("team1_ban1", "").strip(),
            "protect1": request.form.get("team1_protect1", "").strip(),
            "ban2": request.form.get("team1_ban2", "").strip(),
            "ban3": request.form.get("team1_ban3", "").strip(),
            "ban4": request.form.get("team1_ban4", "").strip(),
            "protect2": request.form.get("team1_protect2", "").strip(),
        },
        "team2": {
            "ban1": request.form.get("team2_ban1", "").strip(),
            "ban2": request.form.get("team2_ban2", "").strip(),
            "protect1": request.form.get("team2_protect1", "").strip(),
            "ban3": request.form.get("team2_ban3", "").strip(),
            "protect2": request.form.get("team2_protect2", "").strip(),
            "ban4": request.form.get("team2_ban4", "").strip(),
        },
    }
    save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-notes", methods=["POST"])
def update_notes(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    map_entry["notes"] = request.form.get("notes", "").strip()
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/update-notes", methods=["POST"])
def update_tournament_notes(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    map_entry["notes"] = request.form.get("notes", "").strip()
    save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-vod", methods=["POST"])
def update_vod(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    map_entry["vod_url"] = request.form.get("vod_url", "").strip()
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/update-vod", methods=["POST"])
def update_tournament_vod(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    map_entry["vod_url"] = request.form.get("vod_url", "").strip()
    save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-info", methods=["POST"])
def update_map_info(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    update_map_type_from_form(map_entry)
    participant_one, participant_two = get_scrim_participants(scrim)
    valid_team_ids = {
        participant_one.get("id"),
        participant_two.get("id"),
    }

    side1_team_id = parse_team_id(request.form.get("map_team1_team_id", ""))
    updated_our_team_slot = map_entry.get("our_team_slot", "team1")
    if side1_team_id in valid_team_ids and participant_one.get("id") and participant_two.get("id"):
        side1_is_participant_one = side1_team_id == participant_one.get("id")
        updated_our_team_slot = "team1" if side1_is_participant_one else "team2"
        map_entry["our_team_slot"] = updated_our_team_slot
    
    # Get team-specific scores and build combined score
    score_team1 = request.form.get("score_team1", "").strip()
    score_team2 = request.form.get("score_team2", "").strip()
    score = request.form.get("score", "").strip()
    our_atk = request.form.get("our_attack_score", "").strip()
    enemy_atk = request.form.get("enemy_attack_score", "").strip()
    is_attack_defense_map = map_entry.get("map_name") in ATTACK_DEFENSE_MAPS

    # Attack/defense score input takes priority for non-control maps
    if our_atk or enemy_atk:
        map_entry["our_attack_score"] = our_atk
        map_entry["enemy_attack_score"] = enemy_atk
        if updated_our_team_slot == "team1":
            map_entry["score"] = f"{our_atk}-{enemy_atk}"
        else:
            map_entry["score"] = f"{enemy_atk}-{our_atk}"
    elif is_attack_defense_map:
        if updated_our_team_slot == "team1":
            map_entry["our_attack_score"] = score_team1
            map_entry["enemy_attack_score"] = score_team2
        else:
            map_entry["our_attack_score"] = score_team2
            map_entry["enemy_attack_score"] = score_team1
        map_entry["score"] = build_score_text(score_team1, score_team2, score)
    # Prefer team-specific scores if provided
    elif score_team1 or score_team2:
        map_entry["score"] = f"{score_team1}-{score_team2}".strip("-")
    else:
        map_entry["score"] = score
    
    # Manual result selection (no auto-calculation from score)
    map_entry["result"] = normalize_result_value(request.form.get("result", ""))

    # If result is left blank, infer from attack-defense scores directly (most reliable).
    if not map_entry["result"] and (our_atk or enemy_atk):
        try:
            o = int(our_atk) if our_atk else 0
            e = int(enemy_atk) if enemy_atk else 0
            if o > e:
                map_entry["result"] = "Win"
            elif e > o:
                map_entry["result"] = "Loss"
            else:
                map_entry["result"] = "Draw"
        except ValueError:
            pass

    # If result still blank, infer from score text string.
    if not map_entry["result"] and map_entry.get("score"):
        inferred = infer_result_from_score_text(
            map_entry.get("score", ""),
            slot=map_entry.get("our_team_slot", "team1"),
        )
        if inferred in RESULTS:
            map_entry["result"] = inferred

    # Keep map team identity in map-side order so draft/comp team1/team2 stay aligned.
    if updated_our_team_slot == "team2":
        map_entry["team1_id"] = participant_two.get("id")
        map_entry["team2_id"] = participant_one.get("id")
        map_entry["team1_name"] = participant_two.get("name", "")
        map_entry["team2_name"] = participant_one.get("name", "")
    else:
        map_entry["team1_id"] = participant_one.get("id")
        map_entry["team2_id"] = participant_two.get("id")
        map_entry["team1_name"] = participant_one.get("name", "")
        map_entry["team2_name"] = participant_two.get("name", "")
    has_submaps = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), []))
    if has_submaps:
        map_entry["side"] = request.form.get("side", map_entry.get("side", "")).strip()
    else:
        map_entry["side"] = ""
    save_app_state()
    if request.form.get("next") == "scrim":
        return redirect(url_for("scrim_detail", scrim_id=scrim_id))
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/update-info", methods=["POST"])
def update_tournament_map_info(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    update_map_type_from_form(map_entry)
    map_entry["result"] = normalize_result_value(request.form.get("result", map_entry.get("result", "")))
    map_entry["score"] = request.form.get("score", map_entry.get("score", "")).strip()
    if not map_entry["result"] and map_entry.get("score"):
        inferred = infer_result_from_score_text(
            map_entry.get("score", ""),
            slot=map_entry.get("our_team_slot", "team1"),
        )
        if inferred in RESULTS:
            map_entry["result"] = inferred
    raw_team1_id = request.form.get("team1_tournament_team_id", "")
    raw_team2_id = request.form.get("team2_tournament_team_id", "")
    if raw_team1_id or raw_team2_id:
        team1_tournament_team_id = parse_team_id(raw_team1_id)
        team2_tournament_team_id = parse_team_id(raw_team2_id)
        team1 = get_tournament_team_by_id(tournament_match, team1_tournament_team_id)
        team2 = get_tournament_team_by_id(tournament_match, team2_tournament_team_id)
        if team1 is None or team2 is None:
            flash("Select two tournament teams for this map.", "error")
            return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))
        if team1_tournament_team_id == team2_tournament_team_id:
            flash("Map teams must be different.", "error")
            return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))
        map_entry["team1_tournament_team_id"] = team1_tournament_team_id
        map_entry["team2_tournament_team_id"] = team2_tournament_team_id
        map_entry["team1_name"] = team1["name"]
        map_entry["team2_name"] = team2["name"]
    our_team_slot = request.form.get("our_team_slot", map_entry.get("our_team_slot", "team1")).strip()
    map_entry["our_team_slot"] = our_team_slot if our_team_slot in TEAM_SLOTS else "team1"
    has_submaps = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), []))
    if has_submaps:
        map_entry["side"] = request.form.get("side", map_entry.get("side", "")).strip()
    else:
        map_entry["side"] = ""
    save_app_state()
    if request.form.get("next") == "scrim":
        return redirect(url_for("tournament_detail", tournament_id=tournament_id))
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-comp", methods=["POST"])
def update_comp(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    use_section_sides = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), [])) or map_entry.get("map_name") in ATTACK_DEFENSE_MAPS
    section_count = int(request.form.get("section_count", "1"))
    sections = []
    for s in range(section_count):
        side_value = request.form.get(f"sec_{s}_side", "").strip() if use_section_sides else ""
        if side_value not in SIDES:
            side_value = ""
        sec = {
            "submap": request.form.get(f"sec_{s}_submap", "").strip(),
            "side": side_value,
            "score": build_score_text(
                request.form.get(f"sec_{s}_score_team1", "").strip(),
                request.form.get(f"sec_{s}_score_team2", "").strip(),
                request.form.get(f"sec_{s}_score", "").strip(),
            ),
            "team1": [],
            "team2": [],
        }
        for team in ("team1", "team2"):
            for i in range(6):
                hero = request.form.get(f"sec_{s}_{team}_hero_{i}", "").strip()
                player = request.form.get(f"sec_{s}_{team}_player_{i}", "").strip()
                sec[team].append({"hero": hero, "player": player})
        if team_has_duplicate_heroes(sec["team1"]) or team_has_duplicate_heroes(sec["team2"]):
            flash("Each team roster must use unique heroes in a section.", "error")
            return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))
        auto_assign_section_players_from_heroes(
            scrim,
            map_entry,
            sec,
            is_tournament=False,
        )
        sections.append(sec)
    map_entry["comp"] = sections
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/update-comp", methods=["POST"])
def update_tournament_comp(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    use_section_sides = bool(MAP_SUBMAPS.get(map_entry.get("map_name", ""), [])) or map_entry.get("map_name") in ATTACK_DEFENSE_MAPS
    section_count = int(request.form.get("section_count", "1"))
    sections = []
    for s in range(section_count):
        side_value = request.form.get(f"sec_{s}_side", "").strip() if use_section_sides else ""
        if side_value not in SIDES:
            side_value = ""
        sec = {
            "submap": request.form.get(f"sec_{s}_submap", "").strip(),
            "side": side_value,
            "score": build_score_text(
                request.form.get(f"sec_{s}_score_team1", "").strip(),
                request.form.get(f"sec_{s}_score_team2", "").strip(),
                request.form.get(f"sec_{s}_score", "").strip(),
            ),
            "team1": [],
            "team2": [],
        }
        for team in ("team1", "team2"):
            for i in range(6):
                hero = request.form.get(f"sec_{s}_{team}_hero_{i}", "").strip()
                player = request.form.get(f"sec_{s}_{team}_player_{i}", "").strip()
                sec[team].append({"hero": hero, "player": player})
        if team_has_duplicate_heroes(sec["team1"]) or team_has_duplicate_heroes(sec["team2"]):
            flash("Each team roster must use unique heroes in a section.", "error")
            return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))
        auto_assign_section_players_from_heroes(
            tournament_match,
            map_entry,
            sec,
            is_tournament=True,
        )
        sections.append(sec)
    map_entry["comp"] = sections
    save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/update-comp-section/<int:section_index>", methods=["POST"])
def update_comp_section(scrim_id: int, map_id: int, section_index: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    sections = map_entry.get("comp", [])
    if section_index < 0 or section_index >= len(sections):
        abort(404)

    section = sections[section_index]

    section["submap"] = request.form.get("submap", section.get("submap", "")).strip()
    side_value = request.form.get("side", section.get("side", "")).strip()
    if side_value not in SIDES:
        side_value = ""
    section["side"] = side_value
    if "score_team1" in request.form or "score_team2" in request.form or "score" in request.form:
        section["score"] = build_score_text(
            request.form.get("score_team1", "").strip(),
            request.form.get("score_team2", "").strip(),
            request.form.get("score", section.get("score", "")).strip(),
        )
    
    # Store section result (submap/round), or infer from score text.
    section_result = normalize_result_value(request.form.get("section_result", ""))
    if section_result in RESULTS:
        section["result"] = section_result
    else:
        inferred_submap_result = infer_result_from_score_text(section.get("score", ""), slot="team1")
        if inferred_submap_result in RESULTS:
            section["result"] = inferred_submap_result
        else:
            section.pop("result", None)

    for team in ("team1", "team2"):
        team_slots = []
        for i in range(6):
            hero = request.form.get(f"{team}_hero_{i}", "").strip()
            player = request.form.get(f"{team}_player_{i}", "").strip()
            team_slots.append({"hero": hero, "player": player})
        section[team] = team_slots

    if team_has_duplicate_heroes(section["team1"]) or team_has_duplicate_heroes(section["team2"]):
        flash("Each team roster must use unique heroes in a section.", "error")
        return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))

    auto_assign_section_players_from_heroes(
        scrim,
        map_entry,
        section,
        is_tournament=False,
    )

    map_entry["comp"][section_index] = section
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/update-comp-section/<int:section_index>", methods=["POST"])
def update_tournament_comp_section(tournament_id: int, map_id: int, section_index: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    sections = map_entry.get("comp", [])
    if section_index < 0 or section_index >= len(sections):
        abort(404)

    section = sections[section_index]
    section["submap"] = request.form.get("submap", section.get("submap", "")).strip()
    side_value = request.form.get("side", section.get("side", "")).strip()
    if side_value not in SIDES:
        side_value = ""
    section["side"] = side_value
    if "score_team1" in request.form or "score_team2" in request.form or "score" in request.form:
        section["score"] = build_score_text(
            request.form.get("score_team1", "").strip(),
            request.form.get("score_team2", "").strip(),
            request.form.get("score", section.get("score", "")).strip(),
        )
    
    # Store section result (submap/round), or infer from score text.
    section_result = normalize_result_value(request.form.get("section_result", ""))
    if section_result in RESULTS:
        section["result"] = section_result
    else:
        inferred_submap_result = infer_result_from_score_text(section.get("score", ""), slot="team1")
        if inferred_submap_result in RESULTS:
            section["result"] = inferred_submap_result
        else:
            section.pop("result", None)

    for team in ("team1", "team2"):
        team_slots = []
        for i in range(6):
            hero = request.form.get(f"{team}_hero_{i}", "").strip()
            player = request.form.get(f"{team}_player_{i}", "").strip()
            team_slots.append({"hero": hero, "player": player})
        section[team] = team_slots

    if team_has_duplicate_heroes(section["team1"]) or team_has_duplicate_heroes(section["team2"]):
        flash("Each team roster must use unique heroes in a section.", "error")
        return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))

    auto_assign_section_players_from_heroes(
        tournament_match,
        map_entry,
        section,
        is_tournament=True,
    )

    map_entry["comp"][section_index] = section
    save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/add-comp-section", methods=["POST"])
def add_comp_section(scrim_id: int, map_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)

    def _blank_section(submap: str = "") -> dict:
        return {
            "submap": submap,
            "side": "",
            "score": "",
            "team1": [{"hero": "", "player": ""} for _ in range(6)],
            "team2": [{"hero": "", "player": ""} for _ in range(6)],
        }

    def _next_section_for_map(current_map_entry: dict) -> dict | None:
        sections = current_map_entry.setdefault("comp", [])
        submaps = MAP_SUBMAPS.get(current_map_entry.get("map_name", ""), [])
        if submaps:
            used = {
                (sec.get("submap") or "").strip().lower()
                for sec in sections
                if isinstance(sec, dict)
            }
            requested_submap = (request.form.get("next_submap") or "").strip()
            if requested_submap:
                requested_key = requested_submap.lower()
                if requested_submap in submaps and requested_key not in used:
                    return _blank_section(submap=requested_submap)
                flash("Choose an available sub-map for the next round.", "error")
                return None
            for submap_name in submaps:
                if submap_name.strip().lower() not in used:
                    return _blank_section(submap=submap_name)
            return None
        if len(sections) >= 4:
            return None
        return _blank_section()

    sections = map_entry.setdefault("comp", [])
    next_section = _next_section_for_map(map_entry)
    if next_section is not None:
        sections.append(next_section)
        save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/maps/<int:map_id>/add-comp-section", methods=["POST"])
def add_tournament_match_comp_section(tournament_id: int, match_id: int, map_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)
    map_entry = get_map_or_404(tournament_match, map_id)

    def _blank_section(submap: str = "") -> dict:
        return {
            "submap": submap,
            "side": "",
            "score": "",
            "team1": [{"hero": "", "player": ""} for _ in range(6)],
            "team2": [{"hero": "", "player": ""} for _ in range(6)],
        }

    def _next_section_for_map(current_map_entry: dict) -> dict | None:
        sections = current_map_entry.setdefault("comp", [])
        submaps = MAP_SUBMAPS.get(current_map_entry.get("map_name", ""), [])
        if submaps:
            used = {
                (sec.get("submap") or "").strip().lower()
                for sec in sections
                if isinstance(sec, dict)
            }
            requested_submap = (request.form.get("next_submap") or "").strip()
            if requested_submap:
                requested_key = requested_submap.lower()
                if requested_submap in submaps and requested_key not in used:
                    return _blank_section(submap=requested_submap)
                flash("Choose an available sub-map for the next round.", "error")
                return None
            for submap_name in submaps:
                if submap_name.strip().lower() not in used:
                    return _blank_section(submap=submap_name)
            return None
        if len(sections) >= 4:
            return None
        return _blank_section()

    sections = map_entry.setdefault("comp", [])
    next_section = _next_section_for_map(map_entry)
    if next_section is not None:
        sections.append(next_section)
        save_app_state()
    return redirect(url_for("tournament_match_map_detail", tournament_id=tournament_id, match_id=match_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/add-comp-section", methods=["POST"])
def add_tournament_comp_section(tournament_id: int, map_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)

    def _blank_section(submap: str = "") -> dict:
        return {
            "submap": submap,
            "side": "",
            "score": "",
            "team1": [{"hero": "", "player": ""} for _ in range(6)],
            "team2": [{"hero": "", "player": ""} for _ in range(6)],
        }

    def _next_section_for_map(current_map_entry: dict) -> dict | None:
        sections = current_map_entry.setdefault("comp", [])
        submaps = MAP_SUBMAPS.get(current_map_entry.get("map_name", ""), [])
        if submaps:
            used = {
                (sec.get("submap") or "").strip().lower()
                for sec in sections
                if isinstance(sec, dict)
            }
            requested_submap = (request.form.get("next_submap") or "").strip()
            if requested_submap:
                requested_key = requested_submap.lower()
                if requested_submap in submaps and requested_key not in used:
                    return _blank_section(submap=requested_submap)
                flash("Choose an available sub-map for the next round.", "error")
                return None
            for submap_name in submaps:
                if submap_name.strip().lower() not in used:
                    return _blank_section(submap=submap_name)
            return None
        if len(sections) >= 4:
            return None
        return _blank_section()

    sections = map_entry.setdefault("comp", [])
    next_section = _next_section_for_map(map_entry)
    if next_section is not None:
        sections.append(next_section)
        save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/delete-event/<int:event_id>", methods=["POST"])
def delete_event(scrim_id: int, map_id: int, event_id: int):
    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)
    map_entry["events"] = [e for e in map_entry["events"] if e["id"] != event_id]
    save_app_state()
    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/delete-event/<int:event_id>", methods=["POST"])
def delete_tournament_event(tournament_id: int, map_id: int, event_id: int):
    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)
    map_entry["events"] = [e for e in map_entry["events"] if e["id"] != event_id]
    save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


@app.route("/scrims/<int:scrim_id>/maps/<int:map_id>/add-event", methods=["POST"])
def add_event_to_map(scrim_id: int, map_id: int):
    global NEXT_EVENT_ID

    scrim = get_scrim_or_404(scrim_id)
    map_entry = get_map_or_404(scrim, map_id)

    timestamp = request.form.get("timestamp", "").strip()
    event_type = request.form.get("event_type", "").strip()
    description = request.form.get("description", "").strip()

    event_entry = {
        "id": NEXT_EVENT_ID,
        "timestamp": timestamp,
        "event_type": event_type,
        "description": description,
    }

    map_entry["events"].append(event_entry)
    NEXT_EVENT_ID += 1
    save_app_state()

    return redirect(url_for("map_detail", scrim_id=scrim_id, map_id=map_id))


@app.route("/tournaments/<int:tournament_id>/maps/<int:map_id>/add-event", methods=["POST"])
def add_tournament_event_to_map(tournament_id: int, map_id: int):
    global NEXT_EVENT_ID

    tournament_match = get_tournament_or_404(tournament_id)
    map_entry = get_map_or_404(tournament_match, map_id)

    event_entry = {
        "id": NEXT_EVENT_ID,
        "timestamp": request.form.get("timestamp", "").strip(),
        "event_type": request.form.get("event_type", "").strip(),
        "description": request.form.get("description", "").strip(),
    }

    map_entry["events"].append(event_entry)
    NEXT_EVENT_ID += 1
    save_app_state()
    return redirect(url_for("tournament_map_detail", tournament_id=tournament_id, map_id=map_id))


