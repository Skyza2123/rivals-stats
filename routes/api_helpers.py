# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

@app.route("/api/teams/<int:team_id>/enemies")
def api_get_enemy_teams(team_id: int):
    """API endpoint to get opponent teams for a specific team from global teams db."""
    db = get_db()
    migrate_enemy_teams_to_team_database(db)
    team = db.execute("SELECT id FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    enemy_team_rows = db.execute(
        "SELECT id, name FROM teams WHERE id != ? ORDER BY name COLLATE NOCASE",
        (team_id,),
    ).fetchall()

    return jsonify([{
        "id": row["id"],
        "name": row["name"],
    } for row in enemy_team_rows])


@app.route("/api/teams")
def api_get_teams():
    team_rows = get_db().execute(
        "SELECT id, name FROM teams ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return jsonify([
        {
            "id": row["id"],
            "name": row["name"],
        }
        for row in team_rows
    ])


@app.route("/api/teams/<int:team_id>/saved-drafts", methods=["GET", "POST"])
def api_team_saved_drafts(team_id: int):
    db = get_db()
    team = db.execute("SELECT id FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    if request.method == "GET":
        rows = db.execute(
            """
            SELECT id, draft_name, season, draft_slots_json, created_at
            FROM team_saved_drafts
            WHERE team_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (team_id,),
        ).fetchall()
        drafts = []
        for row in rows:
            try:
                stored_payload = json.loads(row["draft_slots_json"] or "{}")
            except json.JSONDecodeError:
                stored_payload = {}

            mode = "full"
            slots = _sanitize_simulator_draft_slots({})
            concept_slots = _sanitize_one_sided_concept_slots({})

            if isinstance(stored_payload, dict) and "mode" in stored_payload:
                mode = (stored_payload.get("mode") or "full").strip() or "full"
                slots = _sanitize_simulator_draft_slots(stored_payload.get("slots"))
                concept_slots = _sanitize_one_sided_concept_slots(stored_payload.get("concept_slots"))
            else:
                # Legacy rows store full draft slots at the root.
                slots = _sanitize_simulator_draft_slots(stored_payload)

            drafts.append(
                {
                    "id": row["id"],
                    "name": row["draft_name"],
                    "season": row["season"],
                    "mode": mode,
                    "slots": slots,
                    "concept_slots": concept_slots,
                    "created_at": row["created_at"],
                }
            )
        return jsonify(drafts)

    payload = request.get_json(silent=True) or {}
    draft_name = (payload.get("name") or "").strip()
    season = normalize_season_value(payload.get("season", ""))
    mode = (payload.get("mode") or "full").strip() or "full"
    slots = _sanitize_simulator_draft_slots(payload.get("slots"))
    concept_slots = _sanitize_one_sided_concept_slots(payload.get("concept_slots"))

    if not draft_name:
        return jsonify({"error": "Draft name is required."}), 400
    if len(draft_name) > 80:
        return jsonify({"error": "Draft name must be 80 characters or less."}), 400
    if mode == "concept_one_sided":
        if not any(concept_slots.values()):
            return jsonify({"error": "Add at least one concept slot before saving."}), 400
    else:
        mode = "full"
        if not any(slots.values()):
            return jsonify({"error": "Add at least one draft hero before saving."}), 400

    draft_payload = {
        "mode": mode,
        "slots": slots,
        "concept_slots": concept_slots,
    }

    cursor = db.execute(
        """
        INSERT INTO team_saved_drafts (team_id, draft_name, season, draft_slots_json)
        VALUES (?, ?, ?, ?)
        """,
        (team_id, draft_name, season, json.dumps(draft_payload)),
    )
    db.commit()

    return jsonify(
        {
            "id": cursor.lastrowid,
            "name": draft_name,
            "season": season,
            "mode": mode,
            "slots": slots,
            "concept_slots": concept_slots,
        }
    ), 201


@app.route("/api/saved-drafts/<int:draft_id>", methods=["DELETE"])
def api_delete_saved_draft(draft_id: int):
    db = get_db()
    row = db.execute(
        "SELECT id FROM team_saved_drafts WHERE id = ?",
        (draft_id,),
    ).fetchone()
    if row is None:
        abort(404)

    db.execute("DELETE FROM team_saved_drafts WHERE id = ?", (draft_id,))
    db.commit()
    return jsonify({"ok": True})


