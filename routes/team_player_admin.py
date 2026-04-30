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
            (team_id, name, role, is_sub, "", notes),
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
            SET role = ?, is_sub = ?, notes = ?
            WHERE id = ?
            """,
            (role, is_sub, notes, existing["id"]),
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
    notes = request.form.get("notes", "").strip()

    if not name:
        flash("Player name is required.", "error")
        return redirect(url_for("team_detail", team_id=row["team_id"]) + "#roster")

    try:
        db.execute(
            """
            UPDATE players
            SET name = ?, role = ?, is_sub = ?, notes = ?
            WHERE id = ?
            """,
            (name, role, is_sub, notes, player_id),
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


# Team creation (legacy enemy-team route now creates a regular team)
