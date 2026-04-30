# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

@app.route("/teams/<int:team_id>/enemies/create", methods=["POST"])
def create_enemy_team(team_id: int):
    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    logo_path = save_team_logo(request.files.get("logo"), name)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if not name:
        msg = "Team name is required."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("teams"))

    db = get_db()
    try:
        db.execute(
            "INSERT INTO teams (name, notes, logo_path, is_personal) VALUES (?, ?, ?, 0)",
            (name, notes, logo_path),
        )
        db.commit()
        new_team = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (name,)).fetchone()
        if is_ajax:
            return jsonify({"success": f"Team '{name}' created.", "team_id": new_team["id"] if new_team else None}), 200
        flash(f"Team '{name}' created.", "success")
        if new_team:
            return redirect(url_for("team_detail", team_id=new_team["id"]))
    except sqlite3.IntegrityError:
        existing = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (name,)).fetchone()
        msg = "A team with this name already exists."
        if is_ajax:
            return jsonify({"error": msg, "team_id": existing["id"] if existing else None}), 400
        flash(msg, "error")
        if existing:
            return redirect(url_for("team_detail", team_id=existing["id"]))
    return redirect(url_for("teams"))


@app.route("/enemies/<int:enemy_team_id>/edit", methods=["POST"])
def edit_enemy_team(enemy_team_id: int):
    db = get_db()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    enemy_row = db.execute("SELECT name FROM enemy_teams WHERE id = ?", (enemy_team_id,)).fetchone()
    if not enemy_row:
        if is_ajax:
            return jsonify({"error": "Team not found."}), 404
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    migrate_enemy_teams_to_team_database(db)
    team_row = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (enemy_row["name"],)).fetchone()
    if not team_row:
        if is_ajax:
            return jsonify({"error": "Team not found."}), 404
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()
    remove_logo = request.form.get("remove_logo", "").strip() == "1"
    new_logo_path = save_team_logo(request.files.get("logo"), name)

    if not name:
        msg = "Team name is required."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("team_detail", team_id=team_row["id"]))

    try:
        current = db.execute("SELECT logo_path FROM teams WHERE id = ?", (team_row["id"],)).fetchone()
        logo_path = current["logo_path"] if current else ""
        if new_logo_path:
            if logo_path and logo_path != new_logo_path:
                delete_team_logo_file(logo_path)
            logo_path = new_logo_path
        elif remove_logo and logo_path:
            delete_team_logo_file(logo_path)
            logo_path = ""
        db.execute(
            "UPDATE teams SET name = ?, notes = ?, logo_path = ? WHERE id = ?",
            (name, notes, logo_path, team_row["id"]),
        )
        db.commit()
        if is_ajax:
            return jsonify({"success": "Team updated."}), 200
        flash("Team updated.", "success")
    except sqlite3.IntegrityError:
        msg = "A team with that name already exists."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")

    if not is_ajax:
        return redirect(url_for("team_detail", team_id=team_row["id"]))
    return jsonify({"success": "Team updated."}), 200


@app.route("/enemies/<int:enemy_team_id>/delete", methods=["POST"])
def delete_enemy_team(enemy_team_id: int):
    db = get_db()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    enemy_row = db.execute("SELECT name FROM enemy_teams WHERE id = ?", (enemy_team_id,)).fetchone()
    if not enemy_row:
        if is_ajax:
            return jsonify({"success": "Team already removed."}), 200
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    migrate_enemy_teams_to_team_database(db)
    team_row = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (enemy_row["name"],)).fetchone()
    if team_row:
        db.execute("DELETE FROM teams WHERE id = ?", (team_row["id"],))
        db.commit()

    if is_ajax:
        return jsonify({"success": "Team removed."}), 200
    flash("Team deleted.", "success")
    return redirect(url_for("teams"))


@app.route("/enemies/<int:enemy_team_id>/players/create", methods=["POST"])
def create_enemy_player(enemy_team_id: int):
    db = get_db()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    enemy_row = db.execute("SELECT name FROM enemy_teams WHERE id = ?", (enemy_team_id,)).fetchone()
    if not enemy_row:
        if is_ajax:
            return jsonify({"error": "Team not found."}), 404
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    migrate_enemy_teams_to_team_database(db)
    team_row = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (enemy_row["name"],)).fetchone()
    if not team_row:
        if is_ajax:
            return jsonify({"error": "Team not found."}), 404
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    name = request.form.get("name", "").strip()
    role = request.form.get("role", "").strip()
    main_hero = request.form.get("main_hero", "").strip()
    notes = request.form.get("notes", "").strip()

    if not name:
        msg = "Player name is required."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("team_detail", team_id=team_row["id"]) + "#roster")

    try:
        db.execute(
            "INSERT INTO players (team_id, name, role, main_hero, notes) VALUES (?, ?, ?, ?, ?)",
            (team_row["id"], name, role, main_hero, notes),
        )
        db.commit()
        if is_ajax:
            return jsonify({"success": f"Player '{name}' added."}), 200
        flash(f"Player '{name}' added.", "success")
    except sqlite3.IntegrityError:
        msg = "A player with that name already exists on this team."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")

    if not is_ajax:
        return redirect(url_for("team_detail", team_id=team_row["id"]) + "#roster")
    return jsonify({"success": "Player added."}), 200


@app.route("/enemy-players/<int:enemy_player_id>/edit", methods=["POST"])
def edit_enemy_player(enemy_player_id: int):
    db = get_db()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    ep_row = db.execute("SELECT enemy_team_id, name FROM enemy_players WHERE id = ?", (enemy_player_id,)).fetchone()
    if not ep_row:
        if is_ajax:
            return jsonify({"error": "Player not found."}), 404
        flash("Player not found.", "info")
        return redirect(url_for("teams"))

    et_row = db.execute("SELECT name FROM enemy_teams WHERE id = ?", (ep_row["enemy_team_id"],)).fetchone()
    if not et_row:
        if is_ajax:
            return jsonify({"error": "Team not found."}), 404
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    migrate_enemy_teams_to_team_database(db)
    team_row = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (et_row["name"],)).fetchone()
    if not team_row:
        if is_ajax:
            return jsonify({"error": "Team not found."}), 404
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    player_row = db.execute(
        "SELECT id FROM players WHERE team_id = ? AND lower(name) = lower(?)",
        (team_row["id"], ep_row["name"]),
    ).fetchone()
    if not player_row:
        if is_ajax:
            return jsonify({"error": "Player not found."}), 404
        flash("Player not found.", "info")
        return redirect(url_for("team_detail", team_id=team_row["id"]) + "#roster")

    name = request.form.get("name", "").strip()
    role = request.form.get("role", "").strip()
    main_hero = request.form.get("main_hero", "").strip()
    notes = request.form.get("notes", "").strip()

    if not name:
        msg = "Player name is required."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("team_detail", team_id=team_row["id"]) + "#roster")

    try:
        db.execute(
            "UPDATE players SET name = ?, role = ?, main_hero = ?, notes = ? WHERE id = ?",
            (name, role, main_hero, notes, player_row["id"]),
        )
        db.commit()
        if is_ajax:
            return jsonify({"success": "Player updated."}), 200
        flash("Player updated.", "success")
    except sqlite3.IntegrityError:
        msg = "A player with that name already exists on this team."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "error")

    if not is_ajax:
        return redirect(url_for("team_detail", team_id=team_row["id"]) + "#roster")
    return jsonify({"success": "Player updated."}), 200


@app.route("/enemy-players/<int:enemy_player_id>/delete", methods=["POST"])
def delete_enemy_player(enemy_player_id: int):
    db = get_db()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    ep_row = db.execute("SELECT enemy_team_id, name FROM enemy_players WHERE id = ?", (enemy_player_id,)).fetchone()
    if not ep_row:
        if is_ajax:
            return jsonify({"success": "Player already removed."}), 200
        flash("Player not found.", "info")
        return redirect(url_for("teams"))

    et_row = db.execute("SELECT name FROM enemy_teams WHERE id = ?", (ep_row["enemy_team_id"],)).fetchone()
    if not et_row:
        if is_ajax:
            return jsonify({"success": "Team not found."}), 200
        flash("Team not found.", "info")
        return redirect(url_for("teams"))

    migrate_enemy_teams_to_team_database(db)
    team_row = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (et_row["name"],)).fetchone()
    if team_row:
        db.execute(
            "DELETE FROM players WHERE team_id = ? AND lower(name) = lower(?)",
            (team_row["id"], ep_row["name"]),
        )
        db.commit()

    if is_ajax:
        return jsonify({"success": "Player removed."}), 200
    flash("Player removed.", "success")
    if team_row:
        return redirect(url_for("team_detail", team_id=team_row["id"]) + "#roster")
    return redirect(url_for("teams"))


@app.route("/enemies/<int:enemy_team_id>")
def enemy_team_detail(enemy_team_id: int):
    """Legacy route: redirect to the unified team_detail page."""
    db = get_db()
    enemy_row = db.execute("SELECT name FROM enemy_teams WHERE id = ?", (enemy_team_id,)).fetchone()
    if not enemy_row:
        flash("This team is already in the Teams database.", "info")
        return redirect(url_for("teams"))
    migrate_enemy_teams_to_team_database(db)
    db.commit()
    team_row = db.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (enemy_row["name"],)).fetchone()
    if team_row:
        return redirect(url_for("team_detail", team_id=team_row["id"]))
    return redirect(url_for("teams"))


@app.route("/enemies/<int:enemy_team_id>/draft-predict")
def enemy_draft_predict(enemy_team_id: int):
    """Legacy route: return empty predictor data (team is now in main teams table)."""
    return jsonify(build_draft_predictor([], {}))

