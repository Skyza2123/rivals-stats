# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

_JARVIS_STOP_WORDS = {
    "a", "about", "and", "bans", "compare", "for", "go", "hey", "jarvis",
    "load", "me", "of", "on", "open", "please", "prep", "pull", "quick",
    "recent", "show", "take", "team", "the", "to", "trends", "up", "versus",
    "view", "vs", "with",
}


def _jarvis_text_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _jarvis_team_rows() -> list[dict]:
    rows = get_db().execute(
        "SELECT id, name, is_personal FROM teams ORDER BY LENGTH(name) DESC, name COLLATE NOCASE"
    ).fetchall()
    return [
        {"id": int(row["id"]), "name": str(row["name"] or "").strip(), "is_personal": bool(row["is_personal"])}
        for row in rows
        if str(row["name"] or "").strip()
    ]


def _jarvis_mentioned_teams(command: str, teams: list[dict]) -> list[dict]:
    command_key = _jarvis_text_key(command)
    matches = []
    seen = set()
    for team in teams:
        team_key = _jarvis_text_key(team["name"])
        if team_key and team_key in command_key and team["id"] not in seen:
            matches.append((command_key.index(team_key), team))
            seen.add(team["id"])
    return [team for _, team in sorted(matches, key=lambda item: item[0])]


def _jarvis_fuzzy_team(command: str, teams: list[dict]) -> dict | None:
    tokens = [
        token for token in re.findall(r"[a-z0-9]+", str(command or "").lower())
        if token not in _JARVIS_STOP_WORDS
    ]
    if not tokens:
        return None
    candidate = " ".join(tokens)
    scored = sorted(
        [
            (
            max(
                SequenceMatcher(None, candidate, team["name"].lower()).ratio(),
                SequenceMatcher(None, _jarvis_text_key(candidate), _jarvis_text_key(team["name"])).ratio(),
            ),
            team,
            )
            for team in teams
        ],
        key=lambda item: item[0],
    )
    if not scored or scored[-1][0] < 0.72:
        return None
    if len(scored) > 1 and scored[-1][0] - scored[-2][0] < 0.08:
        return None
    return scored[-1][1]


def _jarvis_find_team(command: str, teams: list[dict]) -> dict | None:
    mentioned = _jarvis_mentioned_teams(command, teams)
    return mentioned[0] if mentioned else _jarvis_fuzzy_team(command, teams)


def _jarvis_map_result_counts(scrim: dict) -> tuple[int, int, int]:
    wins = losses = draws = 0
    for map_entry in scrim.get("maps", []):
        result = str(map_entry.get("result") or "").lower()
        if "win" in result:
            wins += 1
        elif "loss" in result:
            losses += 1
        elif "draw" in result or "tie" in result:
            draws += 1
    return wins, losses, draws


def _jarvis_scrim_panel(team: dict, losses_only: bool = False) -> dict:
    rows = []
    for scrim in reversed(get_scrims_for_team(team["id"], team["name"])):
        wins, losses, draws = _jarvis_map_result_counts(scrim)
        if losses_only and losses == 0:
            continue
        opponent = str(scrim.get("enemy_team") or scrim.get("opponent") or "Unknown").strip()
        rows.append(
            {
                "label": f"{scrim.get('scrim_date') or 'No date'} | {team['name']} vs {opponent}",
                "meta": f"{wins}W-{losses}L" + (f"-{draws}D" if draws else ""),
                "url": url_for("scrim_detail", scrim_id=scrim["id"]),
            }
        )
        if len(rows) >= 6:
            break
    title = f"Recent {team['name']} losses" if losses_only else f"Recent {team['name']} scrims"
    return {
        "action": "panel",
        "title": title,
        "message": f"Pulling {title.lower()}.",
        "filters": [team["name"], "losses only" if losses_only else "all results"],
        "items": rows,
        "empty": f"No matching scrims found for {team['name']}.",
    }


def _jarvis_map_ban_panel(command: str) -> dict | None:
    map_name = next((name for name in MAPS if _jarvis_text_key(name) in _jarvis_text_key(command)), None)
    if not map_name:
        return None
    counts: Counter = Counter()
    matching_maps = 0
    for scrim in SCRIMS + TOURNAMENT_MATCHES:
        for map_entry in scrim.get("maps", []):
            if _jarvis_text_key(map_entry.get("map_name", "")) != _jarvis_text_key(map_name):
                continue
            matching_maps += 1
            draft = map_entry.get("draft", {})
            if not isinstance(draft, dict):
                continue
            for team_slot in ("team1", "team2"):
                side = draft.get(team_slot, {})
                if not isinstance(side, dict):
                    continue
                for key in ("ban1", "ban2", "ban3", "ban4"):
                    hero = canonicalize_hero_name(side.get(key, ""))
                    if hero:
                        counts[hero] += 1
    items = [
        {"label": hero, "meta": f"{count} recorded ban{'s' if count != 1 else ''}"}
        for hero, count in counts.most_common(8)
    ]
    return {
        "action": "panel",
        "title": f"Bans on {map_name}",
        "message": f"Pulling bans on {map_name}.",
        "filters": [map_name, f"{matching_maps} matching maps"],
        "items": items,
        "empty": f"No ban data found for {map_name}.",
    }


def _jarvis_navigate(url: str, message: str) -> dict:
    return {"action": "navigate", "url": url, "message": message}


@app.route("/api/jarvis-command", methods=["POST"])
def jarvis_command():
    payload = request.get_json(silent=True) or {}
    command = re.sub(r"^\s*(?:hey\s+)?jarvis[\s,.:;-]*", "", str(payload.get("command") or ""), flags=re.I).strip()
    normalized = command.lower()
    teams = _jarvis_team_rows()
    mentioned = _jarvis_mentioned_teams(command, teams)
    team = mentioned[0] if mentioned else _jarvis_fuzzy_team(command, teams)

    if not command:
        return jsonify({"action": "message", "message": "Online. Awaiting your command."})

    if "latest scrim" in normalized:
        if not SCRIMS:
            return jsonify({"action": "message", "message": "No scrims are available."})
        latest = max(SCRIMS, key=lambda row: (str(row.get("scrim_date") or ""), int(row.get("id") or 0)))
        return jsonify(_jarvis_navigate(url_for("scrim_detail", scrim_id=latest["id"]), "Opening the latest scrim."))

    if "compare" in normalized:
        if len(mentioned) < 2:
            return jsonify({"action": "message", "message": "Which two teams should I compare?"})
        return jsonify(_jarvis_navigate(
            url_for("teams_compare", team_a=mentioned[0]["id"], team_b=mentioned[1]["id"]),
            f"Comparing {mentioned[0]['name']} and {mentioned[1]['name']}.",
        ))

    if ("prep" in normalized or "matchup" in normalized) and team:
        personal = next((row for row in teams if row["is_personal"]), None)
        query = {"tab": "matchup", "team_b": team["id"]}
        if personal and personal["id"] != team["id"]:
            query["team_a"] = personal["id"]
        return jsonify(_jarvis_navigate(url_for("machine", **query), f"Loading matchup prep for {team['name']}."))

    if "ban" in normalized and (" on " in f" {normalized} " or " map " in f" {normalized} "):
        panel = _jarvis_map_ban_panel(command)
        if panel:
            return jsonify(panel)

    if "ban" in normalized and team:
        return jsonify(_jarvis_navigate(url_for("team_detail", team_id=team["id"]) + "#bans", f"Opening {team['name']} ban trends."))

    if any(phrase in normalized for phrase in ("show", "pull up", "quick look", "preview")) and team:
        return jsonify(_jarvis_scrim_panel(team, losses_only="loss" in normalized))

    if ("team" in normalized or "scout" in normalized) and team:
        return jsonify(_jarvis_navigate(url_for("team_detail", team_id=team["id"]), f"Opening {team['name']}."))

    if "scrim" in normalized or "database" in normalized:
        return jsonify(_jarvis_navigate(url_for("scrims"), "Opening the scrim database."))

    if "draft" in normalized:
        return jsonify(_jarvis_navigate(url_for("draft_simulator"), "Opening Draft Prep."))

    if "player" in normalized or "ranking" in normalized or "history" in normalized:
        return jsonify(_jarvis_navigate(url_for("player_rankings"), "Opening player rankings."))

    if "analyst" in normalized or "chat" in normalized:
        return jsonify(_jarvis_navigate(url_for("machine", tab="chat"), "Opening the analyst."))

    return jsonify({
        "action": "message",
        "message": "Clarify the target: team, scrim, map, player, comparison, or draft prep.",
    })
