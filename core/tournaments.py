# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def get_tournament_map_loser_team_id(map_entry: dict) -> int | None:
    left_score, right_score = split_score_pair(map_entry.get("score", ""))
    if left_score.isdigit() and right_score.isdigit():
        left_value = int(left_score)
        right_value = int(right_score)
        if left_value > right_value:
            return map_entry.get("team2_tournament_team_id")
        if right_value > left_value:
            return map_entry.get("team1_tournament_team_id")

    result = str(map_entry.get("result", "")).strip()
    our_team_slot = map_entry.get("our_team_slot", "team1")
    if our_team_slot not in TEAM_SLOTS:
        our_team_slot = "team1"
    other_slot = "team2" if our_team_slot == "team1" else "team1"
    if result == "Win":
        return map_entry.get(f"{other_slot}_tournament_team_id")
    if result == "Loss":
        return map_entry.get(f"{our_team_slot}_tournament_team_id")
    return None


def get_result_for_slot(map_entry: dict, slot: str) -> str:
    result = str(map_entry.get("result", "")).strip()
    if result not in {"Win", "Loss"}:
        return result
    original_slot = map_entry.get("our_team_slot", "team1")
    if original_slot not in TEAM_SLOTS:
        original_slot = "team1"
    if slot == original_slot:
        return result
    return "Loss" if result == "Win" else "Win"


def get_map_outcome_for_slot(map_entry: dict, slot: str) -> str:
    left_score, right_score = split_score_pair(map_entry.get("score", ""))
    if left_score.isdigit() and right_score.isdigit():
        left_value = int(left_score)
        right_value = int(right_score)
        if left_value != right_value:
            winner_slot = "team1" if left_value > right_value else "team2"
            return "Win" if winner_slot == slot else "Loss"
    return get_result_for_slot(map_entry, slot)


def infer_result_from_score_text(score_text: str, *, slot: str = "team1") -> str:
    left_score, right_score = split_score_pair(score_text)
    if not (left_score.isdigit() and right_score.isdigit()):
        return ""
    left_value = int(left_score)
    right_value = int(right_score)
    if left_value == right_value:
        return ""
    winner_slot = "team1" if left_value > right_value else "team2"
    return "Win" if winner_slot == slot else "Loss"


def get_tournament_team_slot_for_map(map_entry: dict, tournament_team_id: int | None) -> str | None:
    if tournament_team_id is None:
        return None
    if map_entry.get("team1_tournament_team_id") == tournament_team_id:
        return "team1"
    if map_entry.get("team2_tournament_team_id") == tournament_team_id:
        return "team2"
    return None


def build_tournament_team_scrims(tournament_record: dict, tournament_team: dict) -> list[dict]:
    tournament_team_id = tournament_team.get("id")
    team_scrims: list[dict] = []
    for tournament_match in tournament_record.get("matches", []):
        if tournament_match.get("team1_tournament_team_id") == tournament_team_id:
            opponent_name = tournament_match.get("team2_name") or "Opponent"
        elif tournament_match.get("team2_tournament_team_id") == tournament_team_id:
            opponent_name = tournament_match.get("team1_name") or "Opponent"
        else:
            continue

        remapped_maps: list[dict] = []
        for original_map in tournament_match.get("maps", []):
            if not isinstance(original_map, dict):
                continue
            team_slot = get_tournament_team_slot_for_map(original_map, tournament_team_id)
            if team_slot is None:
                continue
            map_entry = copy.deepcopy(original_map)
            map_entry["our_team_slot"] = team_slot
            map_entry["result"] = get_result_for_slot(original_map, team_slot)
            inferred_result = infer_result_from_score_text(map_entry.get("score", ""), slot=team_slot)
            if inferred_result:
                map_entry["result"] = inferred_result
            remapped_maps.append(map_entry)

        team_scrims.append(
            {
                "id": tournament_match.get("id"),
                "opponent": opponent_name,
                "enemy_team": opponent_name,
                "scrim_date": tournament_match.get("scrim_date") or tournament_record.get("scrim_date", ""),
                "season": tournament_record.get("season", ""),
                "team_id": tournament_record.get("team_id"),
                "team_name": tournament_team.get("name", ""),
                "notes": tournament_match.get("notes", ""),
                "maps": remapped_maps,
            }
        )
    return team_scrims


def build_tournament_match_scrims(tournament_record: dict, perspective: str = "team1") -> list[dict]:
    perspective = perspective if perspective in TEAM_SLOTS else "team1"
    opponent_slot = "team2" if perspective == "team1" else "team1"
    perspective_id_key = f"{perspective}_tournament_team_id"
    opponent_id_key = f"{opponent_slot}_tournament_team_id"
    perspective_name_key = f"{perspective}_name"
    opponent_name_key = f"{opponent_slot}_name"

    tournament_scrims: list[dict] = []
    for tournament_match in tournament_record.get("matches", []):
        perspective_team_id = tournament_match.get(perspective_id_key)
        if perspective_team_id is None:
            continue

        remapped_maps: list[dict] = []
        for original_map in tournament_match.get("maps", []):
            if not isinstance(original_map, dict):
                continue
            team_slot = get_tournament_team_slot_for_map(original_map, perspective_team_id)
            if team_slot is None:
                continue
            map_entry = copy.deepcopy(original_map)
            map_entry["our_team_slot"] = team_slot
            map_entry["result"] = get_result_for_slot(original_map, team_slot)
            inferred_result = infer_result_from_score_text(map_entry.get("score", ""), slot=team_slot)
            if inferred_result:
                map_entry["result"] = inferred_result
            remapped_maps.append(map_entry)

        tournament_scrims.append(
            {
                "id": tournament_match.get("id"),
                "opponent": tournament_match.get(opponent_name_key) or "Opponent",
                "enemy_team": tournament_match.get(opponent_name_key) or "Opponent",
                "scrim_date": tournament_match.get("scrim_date") or tournament_record.get("scrim_date", ""),
                "season": tournament_record.get("season", ""),
                "team_id": tournament_record.get("team_id"),
                "team_name": tournament_match.get(perspective_name_key) or f"Match {perspective.title()}",
                "notes": tournament_match.get("notes", ""),
                "maps": remapped_maps,
                "team1_tournament_team_id": tournament_match.get(perspective_id_key),
                "team2_tournament_team_id": tournament_match.get(opponent_id_key),
            }
        )

    return tournament_scrims


def build_tournament_team_pick_rows(tournament_record: dict, tournament_team: dict) -> list[dict]:
    pick_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})
    tournament_team_id = tournament_team.get("id")

    for tournament_match in tournament_record.get("matches", []):
        if tournament_match.get("team1_tournament_team_id") != tournament_team_id and tournament_match.get("team2_tournament_team_id") != tournament_team_id:
            continue

        for map_entry in tournament_match.get("maps", []):
            if map_entry.get("picked_by_tournament_team_id") != tournament_team_id:
                continue
            team_slot = get_tournament_team_slot_for_map(map_entry, tournament_team_id)
            if team_slot is None:
                continue
            map_name = str(map_entry.get("map_name", "")).strip()
            if not map_name:
                continue
            pick_stats[map_name]["maps"] += 1
            result = get_map_outcome_for_slot(map_entry, team_slot)
            if result == "Win":
                pick_stats[map_name]["wins"] += 1
            elif result == "Loss":
                pick_stats[map_name]["losses"] += 1

    pick_rows = []
    for map_name, stats in pick_stats.items():
        maps_played = stats["maps"]
        pick_rows.append(
            {
                "map_name": map_name,
                "mode": MAP_MODES.get(map_name, "Other"),
                "maps": maps_played,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round((stats["wins"] / maps_played) * 100, 1) if maps_played else 0,
                "image": get_map_image_url(map_name),
            }
        )
    pick_rows.sort(key=lambda row: (row["maps"], row["win_rate"]), reverse=True)
    return pick_rows


def build_tournament_match_summaries(tournament_record: dict) -> list[dict]:
    match_summaries: list[dict] = []
    for tournament_match in tournament_record.get("matches", []):
        maps_played = len(tournament_match.get("maps", []))
        completed_maps = sum(1 for map_entry in tournament_match.get("maps", []) if map_entry.get("result"))
        picked_maps = sum(1 for map_entry in tournament_match.get("maps", []) if map_entry.get("picked_by_tournament_team_id"))
        match_summaries.append(
            {
                "id": tournament_match.get("id"),
                "team1_name": tournament_match.get("team1_name") or "Team 1",
                "team2_name": tournament_match.get("team2_name") or "Team 2",
                "scrim_date": tournament_match.get("scrim_date") or tournament_record.get("scrim_date", ""),
                "notes": tournament_match.get("notes", ""),
                "maps": maps_played,
                "completed_maps": completed_maps,
                "picked_maps": picked_maps,
            }
        )
    return match_summaries


def build_tournament_overview_analytics(tournament_record: dict) -> dict:
    ban_counts = defaultdict(int)
    protect_counts = defaultdict(int)
    map_stats = defaultdict(
        lambda: {
            "count": 0,
            "completed": 0,
            "wins": 0,
            "losses": 0,
            "mirrored_completed": 0,
            "mirrored_wins": 0,
            "unmirrored_completed": 0,
            "unmirrored_wins": 0,
        }
    )

    total_maps = 0
    total_ban_events = 0
    total_protect_events = 0

    for tournament_match in tournament_record.get("matches", []):
        for map_entry in tournament_match.get("maps", []):
            if not isinstance(map_entry, dict):
                continue

            map_name = str(map_entry.get("map_name", "")).strip()
            if map_name:
                map_stats[map_name]["count"] += 1
                total_maps += 1

            result_value = str(map_entry.get("result", "")).strip()
            if map_name and result_value in {"Win", "Loss"}:
                mirrored = is_map_draft_mirrored(map_entry)
                unmirrored = is_map_draft_unmirrored(map_entry)
                map_stats[map_name]["completed"] += 1
                if result_value == "Win":
                    map_stats[map_name]["wins"] += 1
                else:
                    map_stats[map_name]["losses"] += 1

                if mirrored:
                    map_stats[map_name]["mirrored_completed"] += 1
                    if result_value == "Win":
                        map_stats[map_name]["mirrored_wins"] += 1
                if unmirrored:
                    map_stats[map_name]["unmirrored_completed"] += 1
                    if result_value == "Win":
                        map_stats[map_name]["unmirrored_wins"] += 1

            draft = map_entry.get("draft", {})
            for team_key in ("team1", "team2"):
                team_draft = draft.get(team_key, {}) if isinstance(draft, dict) else {}
                for slot_key in ("ban1", "ban2", "ban3", "ban4"):
                    hero_name = canonicalize_hero_name(team_draft.get(slot_key, ""))
                    if hero_name:
                        ban_counts[hero_name] += 1
                        total_ban_events += 1
                for slot_key in ("protect1", "protect2"):
                    hero_name = canonicalize_hero_name(team_draft.get(slot_key, ""))
                    if hero_name:
                        protect_counts[hero_name] += 1
                        total_protect_events += 1

    ban_rows = []
    for hero_name, count in ban_counts.items():
        ban_rows.append(
            {
                "hero": hero_name,
                "count": count,
                "share": round((count / total_ban_events) * 100, 1) if total_ban_events else 0,
            }
        )
    ban_rows.sort(key=lambda row: (row["count"], row["hero"]), reverse=True)

    protect_rows = []
    for hero_name, count in protect_counts.items():
        protect_rows.append(
            {
                "hero": hero_name,
                "count": count,
                "share": round((count / total_protect_events) * 100, 1) if total_protect_events else 0,
            }
        )
    protect_rows.sort(key=lambda row: (row["count"], row["hero"]), reverse=True)

    map_rows = []
    for map_name, stats in map_stats.items():
        map_rows.append(
            {
                "map_name": map_name,
                "mode": MAP_MODES.get(map_name, "Other"),
                "count": stats["count"],
                "completed": stats["completed"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round((stats["wins"] / stats["completed"]) * 100, 1) if stats["completed"] else None,
                "mirrored_completed": stats["mirrored_completed"],
                "mirrored_win_rate": round((stats["mirrored_wins"] / stats["mirrored_completed"]) * 100, 1) if stats["mirrored_completed"] else None,
                "unmirrored_completed": stats["unmirrored_completed"],
                "unmirrored_win_rate": round((stats["unmirrored_wins"] / stats["unmirrored_completed"]) * 100, 1) if stats["unmirrored_completed"] else None,
                "play_rate": round((stats["count"] / total_maps) * 100, 1) if total_maps else 0,
                "image": get_map_image_url(map_name),
            }
        )
    map_rows.sort(key=lambda row: (row["count"], row["completed"], row["map_name"]), reverse=True)

    return {
        "summary": {
            "total_maps": total_maps,
            "unique_maps": len(map_rows),
            "total_ban_events": total_ban_events,
            "unique_bans": len(ban_rows),
            "total_protect_events": total_protect_events,
            "unique_protects": len(protect_rows),
        },
        "ban_rows": ban_rows[:12],
        "protect_rows": protect_rows[:12],
        "map_rows": map_rows[:12],
    }


