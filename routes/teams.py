from flask import jsonify


def register_team_routes(app, *, is_edit_session, get_db):
    @app.route("/api/teams/<int:team_id>/set-personal", methods=["POST"])
    def api_set_personal_team(team_id: int):
        if not is_edit_session():
            return jsonify({"error": "Unauthorized"}), 403

        db = get_db()
        team = db.execute("SELECT id FROM teams WHERE id = ?", (team_id,)).fetchone()
        if not team:
            return jsonify({"error": "Team not found"}), 404

        db.execute("UPDATE teams SET is_personal = 0")
        db.execute("UPDATE teams SET is_personal = 1 WHERE id = ?", (team_id,))
        db.commit()

        return jsonify({"success": True})
