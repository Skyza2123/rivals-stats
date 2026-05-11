# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def normalize_scrim_record(scrim: dict) -> dict:
    # Auto-assign season from scrim_date if season is empty
    season = normalize_season_value(scrim.get("season", ""))
    if not season:
        season = _get_season_from_date(scrim.get("scrim_date", ""))
    scrim["season"] = season
    scrim["team_slot"] = normalize_match_team_slot(scrim.get("team_slot", "team1"))
    if not scrim.get("enemy_team") and scrim.get("opponent"):
        scrim["enemy_team"] = scrim.get("opponent", "")
    if not scrim.get("opponent") and scrim.get("enemy_team"):
        scrim["opponent"] = scrim.get("enemy_team", "")
    scrim.setdefault("team1_id", scrim.get("team_id"))
    scrim.setdefault("team2_id", scrim.get("enemy_team_id"))
    scrim["team1_players"] = parse_name_list("\n".join(scrim.get("team1_players", [])))
    scrim["team2_players"] = parse_name_list("\n".join(scrim.get("team2_players", [])))
    if not scrim.get("team1_name"):
        scrim["team1_name"] = str(scrim.get("team_name", "")).strip()
    if not scrim.get("team2_name"):
        scrim["team2_name"] = str(scrim.get("enemy_team") or scrim.get("opponent") or "").strip()
    for map_entry in scrim.get("maps", []):
        if not isinstance(map_entry, dict):
            continue
        draft = map_entry.get("draft", {})
        if isinstance(draft, dict):
            for side in TEAM_SLOTS:
                team_draft = draft.get(side, {})
                if not isinstance(team_draft, dict):
                    continue
                for slot_key, hero_name in list(team_draft.items()):
                    team_draft[slot_key] = normalize_hero_slot_value(hero_name)

        comp_sections = map_entry.get("comp", [])
        if isinstance(comp_sections, list):
            for section in comp_sections:
                if not isinstance(section, dict):
                    continue
                for side in TEAM_SLOTS:
                    slots = section.get(side, [])
                    if not isinstance(slots, list):
                        continue
                    for slot in slots:
                        if not isinstance(slot, dict):
                            continue
                        raw_player_name = str(slot.get("player", "")).strip()
                        if is_ringer_player_name(raw_player_name):
                            slot["player"] = ""
                        else:
                            slot["player"] = normalize_player_name(raw_player_name)
                        slot["hero"] = normalize_hero_slot_value(slot.get("hero", ""))
    return scrim


def normalize_tournament_record(match: dict) -> dict:
    match["season"] = normalize_season_value(match.get("season", ""))
    match["team_slot"] = normalize_match_team_slot(match.get("team_slot", "team1"))
    match.setdefault("notes", "")
    match.setdefault("maps", [])
    match.setdefault("matches", [])
    match.setdefault("team_id", None)
    match.setdefault("team_name", "")
    match.setdefault("tournament_name", "")
    match.setdefault("tournament_teams", [])
    match.setdefault("team1_enemy_id", None)
    match.setdefault("team1_name", "")
    match.setdefault("team1_players", [])
    match.setdefault("team2_enemy_id", None)
    match.setdefault("team2_name", "")
    match.setdefault("team2_players", [])
    match["team1_players"] = parse_name_list("\n".join(str(player) for player in match.get("team1_players", [])))
    match["team2_players"] = parse_name_list("\n".join(str(player) for player in match.get("team2_players", [])))

    normalized_teams: list[dict] = []
    next_team_id = 1
    for team in match.get("tournament_teams", []):
        if not isinstance(team, dict):
            continue
        name = str(team.get("name", "")).strip()
        if not name:
            continue
        raw_id = team.get("id")
        team_id = raw_id if isinstance(raw_id, int) and raw_id > 0 else next_team_id
        next_team_id = max(next_team_id, team_id + 1)
        players = parse_name_list("\n".join(str(player) for player in team.get("players", [])))
        normalized_team = {
            "id": team_id,
            "name": name,
            "players": players,
        }
        source_team_id = team.get("source_team_id")
        if isinstance(source_team_id, int):
            normalized_team["source_team_id"] = source_team_id
        normalized_teams.append(normalized_team)

    if not normalized_teams:
        if match.get("team1_name"):
            normalized_teams.append({
                "id": next_team_id,
                "name": str(match.get("team1_name", "")).strip(),
                "players": list(match.get("team1_players", [])),
            })
            next_team_id += 1
        if match.get("team2_name") and str(match.get("team2_name", "")).strip().lower() != str(match.get("team1_name", "")).strip().lower():
            normalized_teams.append({
                "id": next_team_id,
                "name": str(match.get("team2_name", "")).strip(),
                "players": list(match.get("team2_players", [])),
            })

    match["tournament_teams"] = normalized_teams

    normalized_matches: list[dict] = []
    next_match_id = 1
    for tournament_match in match.get("matches", []):
        if not isinstance(tournament_match, dict):
            continue
        raw_id = tournament_match.get("id")
        tournament_match_id = raw_id if isinstance(raw_id, int) and raw_id > 0 else next_match_id
        next_match_id = max(next_match_id, tournament_match_id + 1)
        tournament_match["id"] = tournament_match_id
        normalized_matches.append(normalize_tournament_match_record(tournament_match, normalized_teams))

    if not normalized_matches and match.get("maps"):
        grouped_matches: dict[tuple, dict] = {}
        for map_entry in match.get("maps", []):
            if not isinstance(map_entry, dict):
                continue

            team1_name = str(map_entry.get("team1_name", "")).strip() or str(match.get("team1_name", "")).strip() or "Team 1"
            team2_name = str(map_entry.get("team2_name", "")).strip() or str(match.get("team2_name", "")).strip() or "Team 2"
            team1_id = map_entry.get("team1_tournament_team_id") if isinstance(map_entry.get("team1_tournament_team_id"), int) else None
            team2_id = map_entry.get("team2_tournament_team_id") if isinstance(map_entry.get("team2_tournament_team_id"), int) else None
            match_key = (
                team1_id or 0,
                team1_name.lower(),
                team2_id or 0,
                team2_name.lower(),
            )
            generated_match = grouped_matches.get(match_key)
            if generated_match is None:
                generated_match = {
                    "id": next_match_id,
                    "scrim_date": match.get("scrim_date", ""),
                    "notes": "",
                    "team1_tournament_team_id": team1_id,
                    "team2_tournament_team_id": team2_id,
                    "team1_name": team1_name,
                    "team2_name": team2_name,
                    "maps": [],
                }
                grouped_matches[match_key] = generated_match
                next_match_id += 1
            generated_match["maps"].append(map_entry)
        normalized_matches = [
            normalize_tournament_match_record(tournament_match, normalized_teams)
            for tournament_match in grouped_matches.values()
        ]

    match["matches"] = normalized_matches
    match["maps"] = []

    return match


def _scrim_side_team_key(scrim: dict, side: str) -> str:
    side_id = scrim.get(f"{side}_id")
    if side_id:
        return f"id:{side_id}"
    side_name = (scrim.get(f"{side}_name") or "").strip().lower()
    return f"name:{side_name}"


def _build_existing_scrim_pair_history(scrims: list[dict]) -> dict[str, dict[tuple[str, str], int]]:
    history: dict[str, dict[tuple[str, str], int]] = defaultdict(lambda: defaultdict(int))

    for scrim in scrims:
        if not isinstance(scrim, dict):
            continue
        for map_entry in scrim.get("maps", []):
            if not isinstance(map_entry, dict):
                continue
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for side in TEAM_SLOTS:
                    team_key = _scrim_side_team_key(scrim, side)
                    if team_key.endswith("name:"):
                        continue
                    for slot in section.get(side, []):
                        if not isinstance(slot, dict):
                            continue
                        hero_name = canonicalize_hero_name(slot.get("hero", ""))
                        player_name = (slot.get("player") or "").strip()
                        if not hero_name or not player_name:
                            continue
                        history[team_key][(hero_name, player_name)] += 1

    return history


def auto_assign_existing_scrims(scrims: list[dict]) -> bool:
    history = _build_existing_scrim_pair_history(scrims)
    changed = False

    for scrim in scrims:
        if not isinstance(scrim, dict):
            continue

        roster_by_side = {
            "team1": [str(name).strip() for name in scrim.get("team1_players", []) if str(name).strip()],
            "team2": [str(name).strip() for name in scrim.get("team2_players", []) if str(name).strip()],
        }

        for map_entry in scrim.get("maps", []):
            if not isinstance(map_entry, dict):
                continue
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue

                for side in TEAM_SLOTS:
                    slots = section.get(side, [])
                    if not isinstance(slots, list):
                        continue

                    roster = roster_by_side.get(side, [])
                    if not roster:
                        continue

                    team_key = _scrim_side_team_key(scrim, side)
                    pair_counts = history.get(team_key, {})
                    used_players: set[str] = set()

                    for slot in slots:
                        if not isinstance(slot, dict):
                            continue
                        hero_name = canonicalize_hero_name(slot.get("hero", ""))
                        if not hero_name:
                            continue

                        best_player = None
                        best_count = -1
                        for player_name in roster:
                            if player_name in used_players:
                                continue
                            count = pair_counts.get((hero_name, player_name), 0)
                            if count > best_count:
                                best_count = count
                                best_player = player_name

                        if not best_player:
                            best_player = next((name for name in roster if name not in used_players), None)

                        if best_player and (slot.get("player") or "").strip() != best_player:
                            slot["player"] = best_player
                            changed = True
                        if best_player:
                            used_players.add(best_player)

    return changed


def normalize_tournament_match_record(tournament_match: dict, tournament_teams: list[dict]) -> dict:
    tournament_match.setdefault("notes", "")
    tournament_match.setdefault("maps", [])
    tournament_match.setdefault("scrim_date", "")
    tournament_match.setdefault("team1_tournament_team_id", None)
    tournament_match.setdefault("team2_tournament_team_id", None)
    tournament_match.setdefault("team1_name", "")
    tournament_match.setdefault("team2_name", "")

    team1 = find_tournament_team_by_id(tournament_teams, tournament_match.get("team1_tournament_team_id"))
    team2 = find_tournament_team_by_id(tournament_teams, tournament_match.get("team2_tournament_team_id"))

    if team1 is not None:
        tournament_match["team1_name"] = team1.get("name", "")
    elif not tournament_match.get("team1_tournament_team_id") and tournament_match.get("team1_name"):
        inferred_team = find_tournament_team_by_name(tournament_teams, tournament_match.get("team1_name", ""))
        if inferred_team is not None:
            tournament_match["team1_tournament_team_id"] = inferred_team["id"]
            tournament_match["team1_name"] = inferred_team["name"]

    if team2 is not None:
        tournament_match["team2_name"] = team2.get("name", "")
    elif not tournament_match.get("team2_tournament_team_id") and tournament_match.get("team2_name"):
        inferred_team = find_tournament_team_by_name(tournament_teams, tournament_match.get("team2_name", ""))
        if inferred_team is not None:
            tournament_match["team2_tournament_team_id"] = inferred_team["id"]
            tournament_match["team2_name"] = inferred_team["name"]

    for map_entry in tournament_match.get("maps", []):
        if not isinstance(map_entry, dict):
            continue
        map_entry.setdefault("team1_tournament_team_id", tournament_match.get("team1_tournament_team_id"))
        map_entry.setdefault("team2_tournament_team_id", tournament_match.get("team2_tournament_team_id"))
        map_entry.setdefault("team1_name", tournament_match.get("team1_name", ""))
        map_entry.setdefault("team2_name", tournament_match.get("team2_name", ""))
        map_entry.setdefault("picked_by_tournament_team_id", None)
        map_entry.setdefault("picked_by_name", "")

        team1_map_team = find_tournament_team_by_id(tournament_teams, map_entry.get("team1_tournament_team_id"))
        team2_map_team = find_tournament_team_by_id(tournament_teams, map_entry.get("team2_tournament_team_id"))
        if team1_map_team is not None:
            map_entry["team1_name"] = team1_map_team["name"]
        if team2_map_team is not None:
            map_entry["team2_name"] = team2_map_team["name"]

        if map_entry.get("picked_by_tournament_team_id") is None and map_entry.get("picked_by_name"):
            picker = find_tournament_team_by_name(tournament_teams, map_entry.get("picked_by_name", ""))
            if picker is not None:
                map_entry["picked_by_tournament_team_id"] = picker["id"]
        picker = find_tournament_team_by_id(tournament_teams, map_entry.get("picked_by_tournament_team_id"))
        map_entry["picked_by_name"] = picker.get("name", "") if picker is not None else str(map_entry.get("picked_by_name", "")).strip()

        draft = map_entry.get("draft", {})
        if isinstance(draft, dict):
            for side in TEAM_SLOTS:
                team_draft = draft.get(side, {})
                if not isinstance(team_draft, dict):
                    continue
                for slot_key, hero_name in list(team_draft.items()):
                    team_draft[slot_key] = normalize_hero_slot_value(hero_name)

        comp_sections = map_entry.get("comp", [])
        if isinstance(comp_sections, list):
            for section in comp_sections:
                if not isinstance(section, dict):
                    continue
                for side in TEAM_SLOTS:
                    slots = section.get(side, [])
                    if not isinstance(slots, list):
                        continue
                    for slot in slots:
                        if not isinstance(slot, dict):
                            continue
                        raw_player_name = str(slot.get("player", "")).strip()
                        if is_ringer_player_name(raw_player_name):
                            slot["player"] = ""
                        else:
                            slot["player"] = normalize_player_name(raw_player_name)
                        slot["hero"] = normalize_hero_slot_value(slot.get("hero", ""))

    return tournament_match


def get_scrim_season_options(scrims: list[dict]) -> list[str]:
    def _map_has_played_data(map_entry: dict) -> bool:
        if not isinstance(map_entry, dict):
            return False
        result = str(map_entry.get("result", "")).strip().lower()
        if result in ("win", "loss"):
            return True
        score_text = str(map_entry.get("score", "")).strip()
        if re.search(r"\d+\s*[-:]\s*\d+", score_text):
            return True
        for section in map_entry.get("comp", []) or []:
            if not isinstance(section, dict):
                continue
            for side in TEAM_SLOTS:
                for slot in section.get(side, []) or []:
                    if isinstance(slot, dict) and str(slot.get("hero", "")).strip():
                        return True
        return False

    def _record_has_played_maps(record: dict) -> bool:
        maps = record.get("maps", []) or []
        if any(_map_has_played_data(m) for m in maps if isinstance(m, dict)):
            return True
        for match in record.get("matches", []) or []:
            if not isinstance(match, dict):
                continue
            nested_maps = match.get("maps", []) or []
            if any(_map_has_played_data(m) for m in nested_maps if isinstance(m, dict)):
                return True
        return False

    seasons = {
        normalize_season_value(scrim.get("season", ""))
        for scrim in scrims
        if normalize_season_value(scrim.get("season", "")) and _record_has_played_maps(scrim)
    }
    return sorted(seasons, key=lambda value: [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)])


def get_current_season_from_recent_scrim(scrims: list[dict]) -> str:
    def _map_has_played_data(map_entry: dict) -> bool:
        if not isinstance(map_entry, dict):
            return False
        result = str(map_entry.get("result", "")).strip().lower()
        if result in ("win", "loss"):
            return True
        score_text = str(map_entry.get("score", "")).strip()
        if re.search(r"\d+\s*[-:]\s*\d+", score_text):
            return True
        for section in map_entry.get("comp", []) or []:
            if not isinstance(section, dict):
                continue
            for side in TEAM_SLOTS:
                for slot in section.get(side, []) or []:
                    if isinstance(slot, dict) and str(slot.get("hero", "")).strip():
                        return True
        return False

    def _record_has_played_maps(record: dict) -> bool:
        maps = record.get("maps", []) or []
        if any(_map_has_played_data(m) for m in maps if isinstance(m, dict)):
            return True
        for match in record.get("matches", []) or []:
            if not isinstance(match, dict):
                continue
            nested_maps = match.get("maps", []) or []
            if any(_map_has_played_data(m) for m in nested_maps if isinstance(m, dict)):
                return True
        return False

    for scrim in reversed(scrims):
        season = normalize_season_value(scrim.get("season", ""))
        if season and _record_has_played_maps(scrim):
            return season
    return "all"


def get_selected_season(
    raw_value: str,
    season_options: list[str],
    *,
    allow_unspecified: bool = False,
    default_season: str = "all",
    strict: bool = False,
) -> str:
    selected = normalize_season_value(raw_value)
    if not selected or selected.lower() == "all":
        normalized_default = normalize_season_value(default_season)
        if normalized_default == UNSPECIFIED_SEASON_TOKEN and allow_unspecified:
            return UNSPECIFIED_SEASON_TOKEN
        if normalized_default in season_options:
            return normalized_default
        return "all"
    if selected == UNSPECIFIED_SEASON_TOKEN and allow_unspecified:
        return UNSPECIFIED_SEASON_TOKEN
    if strict:
        return selected
    return selected if selected in season_options else "all"


def filter_scrims_by_season(scrims: list[dict], season: str) -> list[dict]:
    selected = normalize_season_value(season)
    if not selected or selected.lower() == "all":
        return scrims
    if selected == UNSPECIFIED_SEASON_TOKEN:
        return [scrim for scrim in scrims if not normalize_season_value(scrim.get("season", ""))]
    return [scrim for scrim in scrims if normalize_season_value(scrim.get("season", "")) == selected]


def get_selected_map_type(raw_value: str) -> str:
    raw = (raw_value or "").strip()
    if not raw or raw.lower() == "all":
        return "all"

    normalized = normalize_map_type_value(raw)
    return normalized if normalized in MAP_TYPES else "all"


def filter_scrims_by_map_type(scrims: list[dict], selected_map_type: str) -> list[dict]:
    selected = get_selected_map_type(selected_map_type)
    if selected == "all":
        return scrims

    filtered_scrims: list[dict] = []
    for scrim in scrims:
        if not isinstance(scrim, dict):
            continue
        filtered_maps = [
            map_entry
            for map_entry in scrim.get("maps", [])
            if isinstance(map_entry, dict)
            and normalize_map_type_value(map_entry.get("map_type", "")) == selected
        ]
        if not filtered_maps:
            continue
        filtered_scrim = dict(scrim)
        filtered_scrim["maps"] = filtered_maps
        filtered_scrims.append(filtered_scrim)

    return filtered_scrims


