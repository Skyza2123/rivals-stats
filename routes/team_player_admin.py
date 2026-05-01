# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

@app.route("/teams/<int:team_id>/players/create", methods=["POST"])
def create_player(team_id: int):
    db = get_db()
    team_exists = db.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team_exists is None:
        abort(404)

    name = request.form.get("name", "").strip()
    role = normalize_player_role(request.form.get("role", ""))
    is_sub = 1 if request.form.get("is_sub") == "1" else 0
    main_hero = normalize_hero_slot_value(request.form.get("main_hero", ""))
    notes = request.form.get("notes", "").strip()

    if not name:
        flash("Player name is required.", "error")
        return redirect(url_for("team_detail", team_id=team_id) + "#roster")

    try:
        db.execute(
            """
            INSERT INTO players (team_id, name, role, is_sub, main_hero, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (team_id, name, role, is_sub, main_hero, notes),
        )
        db.commit()
    except sqlite3.IntegrityError:
        existing = db.execute(
            "SELECT id FROM players WHERE team_id = ? AND lower(name) = lower(?)",
            (team_id, name),
        ).fetchone()
        if existing is None:
            flash("A player with that name already exists on this team.", "error")
            return redirect(url_for("team_detail", team_id=team_id) + "#roster")
        db.execute(
            """
            UPDATE players
            SET role = ?, is_sub = ?, main_hero = ?, notes = ?
            WHERE id = ?
            """,
            (role, is_sub, main_hero, notes, existing["id"]),
        )
        db.commit()
        flash("Player already existed, so their details were updated.", "success")
        return redirect(url_for("team_detail", team_id=team_id) + "#roster")

    flash("Player added.", "success")
    return redirect(url_for("team_detail", team_id=team_id) + "#roster")


@app.route("/players/<int:player_id>/edit", methods=["POST"])
def edit_player(player_id: int):
    db = get_db()
    row = db.execute("SELECT team_id FROM players WHERE id = ?", (player_id,)).fetchone()
    if row is None:
        abort(404)

    name = request.form.get("name", "").strip()
    role = normalize_player_role(request.form.get("role", ""))
    is_sub = 1 if request.form.get("is_sub") == "1" else 0
    main_hero = normalize_hero_slot_value(request.form.get("main_hero", ""))
    notes = request.form.get("notes", "").strip()

    if not name:
        flash("Player name is required.", "error")
        return redirect(url_for("team_detail", team_id=row["team_id"]) + "#roster")

    try:
        db.execute(
            """
            UPDATE players
            SET name = ?, role = ?, is_sub = ?, main_hero = ?, notes = ?
            WHERE id = ?
            """,
            (name, role, is_sub, main_hero, notes, player_id),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash("A player with that name already exists on this team.", "error")
        return redirect(url_for("team_detail", team_id=row["team_id"]) + "#roster")

    flash("Player updated.", "success")
    return redirect(url_for("team_detail", team_id=row["team_id"]) + "#roster")


@app.route("/players/<int:player_id>/delete", methods=["POST"])
def delete_player(player_id: int):
    db = get_db()
    row = db.execute("SELECT team_id FROM players WHERE id = ?", (player_id,)).fetchone()
    if row is None:
        abort(404)

    db.execute("DELETE FROM players WHERE id = ?", (player_id,))
    db.commit()
    flash("Player removed.", "success")
    return redirect(url_for("team_detail", team_id=row["team_id"]) + "#roster")


@app.route("/teams/<int:team_id>/delete", methods=["POST"])
def delete_team(team_id: int):
    db = get_db()
    deleted = db.execute("DELETE FROM teams WHERE id = ?", (team_id,)).rowcount
    db.commit()
    if not deleted:
        abort(404)
    flash("Team deleted.", "success")
    return redirect(url_for("teams"))


@app.route("/teams/<int:team_id>/players/rename-in-scrims", methods=["POST"])
def rename_player_in_scrims(team_id: int):
    if not _is_edit_session():
        abort(403)
    db = get_db()
    team = db.execute("SELECT id, name FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    from_name = request.form.get("from_name", "").strip()
    to_name = request.form.get("to_name", "").strip()
    if not from_name or not to_name:
        flash("Both old name and new name are required.", "error")
        return redirect(url_for("team_detail", team_id=team_id) + "#roster")
    if from_name.lower() == to_name.lower():
        flash("Old name and new name are the same.", "error")
        return redirect(url_for("team_detail", team_id=team_id) + "#roster")

    from_lower = from_name.lower()
    renamed_slots = 0
    team_scrims = get_scrims_for_team(team["id"], team["name"])
    scrim_ids_to_update = {scrim.get("id") for scrim in team_scrims if scrim.get("id") is not None}
    team_name_lower = (team["name"] or "").strip().lower()

    def _side_for_team(map_entry: dict, fallback_slot: str) -> str:
        if map_entry.get("team1_id") == team_id:
            return "team1"
        if map_entry.get("team2_id") == team_id:
            return "team2"
        if (map_entry.get("team1_name") or "").strip().lower() == team_name_lower:
            return "team1"
        if (map_entry.get("team2_name") or "").strip().lower() == team_name_lower:
            return "team2"
        return fallback_slot if fallback_slot in TEAM_SLOTS else "team1"

    for scrim in SCRIMS:
        if scrim.get("id") not in scrim_ids_to_update:
            continue
        fallback_slot = "team1"
        if scrim.get("team2_id") == team_id or (scrim.get("team2_name") or "").strip().lower() == team_name_lower:
            fallback_slot = "team2"
        elif scrim.get("enemy_team_id") == team_id or (scrim.get("enemy_team") or "").strip().lower() == team_name_lower:
            fallback_slot = opposite_team_slot(normalize_match_team_slot(scrim.get("team_slot", "team1")))
        elif scrim.get("team_id") == team_id or (scrim.get("team_name") or "").strip().lower() == team_name_lower:
            fallback_slot = normalize_match_team_slot(scrim.get("team_slot", "team1"))

        for map_entry in scrim.get("maps", []):
            our_slot = _side_for_team(map_entry, fallback_slot)
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(our_slot, []):
                    if not isinstance(slot, dict):
                        continue
                    if (slot.get("player") or "").strip().lower() == from_lower:
                        slot["player"] = to_name
                        renamed_slots += 1

    if renamed_slots:
        save_app_state()
        # Also merge/update the player DB record if the old name exists.
        existing_from = db.execute(
            "SELECT id FROM players WHERE team_id = ? AND lower(name) = lower(?)",
            (team_id, from_name),
        ).fetchone()
        existing_to = db.execute(
            "SELECT id FROM players WHERE team_id = ? AND lower(name) = lower(?)",
            (team_id, to_name),
        ).fetchone()
        if existing_from is not None and existing_to is not None and existing_from["id"] != existing_to["id"]:
            db.execute("DELETE FROM players WHERE id = ?", (existing_from["id"],))
        elif existing_from is not None:
            db.execute(
                "UPDATE players SET name = ? WHERE id = ?",
                (to_name, existing_from["id"]),
            )
        db.commit()
        flash(f"Renamed \"{from_name}\" to \"{to_name}\" in {renamed_slots} comp slot(s).", "success")
    else:
        flash(f"No comp slots found with player name \"{from_name}\" for this team.", "error")

    return redirect(url_for("team_detail", team_id=team_id) + "#roster")


# Team creation (legacy enemy-team route now creates a regular team)
