# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def _parse_scrim_date(raw_value: str) -> date | None:
    text = (raw_value or "").strip()
    if not text:
        return None

    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# Season midpoint dates used to infer recency for scrims that lack an explicit date.
# Keys are normalized season strings; dates are midpoints of the known season window.
_SEASON_MIDPOINT_DATES: dict[str, date] = {
    "6": date(2026, 1, 29),    # Season 6: Jan 16 - Feb 12
    "6.5": date(2026, 3, 1),   # Season 6.5: Feb 13 - Mar 19
    "7": date(2026, 4, 7),     # Season 7: Mar 20 - present (~Apr 24)
}


def _infer_date_from_season(season_raw: str) -> date | None:
    """Return the midpoint date for a known season string, or None if unrecognised."""
    key = normalize_season_value(season_raw).strip()
    return _SEASON_MIDPOINT_DATES.get(key)


def _get_season_from_date(scrim_date_str: str) -> str:
    """Determine season from scrim_date based on season windows:
    - Season 7: March 20 and after
    - Season 6.5: Feb 13 to March 19
    - Season 6: Jan 16 to Feb 12
    """
    parsed_date = _parse_scrim_date(scrim_date_str)
    if not parsed_date:
        return ""
    
    # Season 7: March 20 and after
    if parsed_date >= date(2026, 3, 20):
        return "7"
    # Season 6.5: Feb 13 to March 19
    elif parsed_date >= date(2026, 2, 13):
        return "6.5"
    # Season 6: Jan 16 to Feb 12
    elif parsed_date >= date(2026, 1, 16):
        return "6"
    return ""


def normalize_season_value(raw_value: str) -> str:
    value = " ".join((raw_value or "").strip().split())
    if not value:
        return ""
    if value == UNSPECIFIED_SEASON_TOKEN:
        return UNSPECIFIED_SEASON_TOKEN

    lower_value = value.lower()
    if lower_value == "all":
        return "all"

    def _normalize_numeric_season_token(token: str) -> str:
        try:
            numeric = float(token)
        except (TypeError, ValueError):
            return token
        if numeric.is_integer():
            return str(int(numeric))
        return f"{numeric:.6f}".rstrip("0").rstrip(".")

    season_token_match = re.search(r"(?:^|\b)s(?:eason)?\s*([0-9]+(?:\.[0-9]+)?)\b", lower_value)
    if season_token_match:
        return _normalize_numeric_season_token(season_token_match.group(1))

    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", lower_value):
        return _normalize_numeric_season_token(lower_value)

    return value


def normalize_match_team_slot(raw_value: str) -> str:
    value = str(raw_value or "").strip()
    return value if value in TEAM_SLOTS else "team1"


def get_scrim_participant_labels(scrim: dict) -> tuple[str, str]:
    team1_name = str(scrim.get("team1_name") or "").strip()
    team2_name = str(scrim.get("team2_name") or "").strip()
    if team1_name or team2_name:
        return team1_name or "This Team", team2_name or "That Team"

    our_label = str(scrim.get("team_name", "")).strip() or "Your Team"
    enemy_label = str(scrim.get("enemy_team") or scrim.get("opponent") or "").strip() or "Enemy Team"
    if normalize_match_team_slot(scrim.get("team_slot", "team1")) == "team2":
        return enemy_label, our_label
    return our_label, enemy_label


def get_scrim_participants(scrim: dict) -> tuple[dict, dict]:
    team1_id = scrim.get("team1_id")
    team2_id = scrim.get("team2_id")
    team1_name, team2_name = get_scrim_participant_labels(scrim)

    if not team1_id and scrim.get("team_id"):
        team1_id = scrim.get("team_id")
    if not team2_id and scrim.get("enemy_team_id"):
        team2_id = scrim.get("enemy_team_id")

    # Canonicalize IDs by name against the teams table to avoid legacy id drift.
    db = get_db()

    def _resolve_team_id_by_name(name: str) -> int | None:
        normalized = (name or "").strip()
        if not normalized:
            return None
        row = db.execute(
            "SELECT id FROM teams WHERE lower(name) = lower(?)",
            (normalized,),
        ).fetchone()
        return int(row["id"]) if row is not None else None

    resolved_team1_id = _resolve_team_id_by_name(team1_name)
    resolved_team2_id = _resolve_team_id_by_name(team2_name)
    if resolved_team1_id:
        team1_id = resolved_team1_id
    if resolved_team2_id:
        team2_id = resolved_team2_id

    # If side labels are different, never keep both sides on the same id.
    if (
        (team1_name or "").strip().lower() != (team2_name or "").strip().lower()
        and team1_id
        and team1_id == team2_id
    ):
        if resolved_team2_id and resolved_team2_id != team1_id:
            team2_id = resolved_team2_id
        elif resolved_team1_id and resolved_team1_id != team2_id:
            team1_id = resolved_team1_id
        else:
            team2_id = None

    return (
        {"id": team1_id, "name": team1_name},
        {"id": team2_id, "name": team2_name},
    )


def get_map_side_default_players(
    match_record: dict,
    map_entry: dict,
    *,
    is_tournament: bool,
    tournament_record: dict | None = None,
) -> dict[str, list[str]]:
    defaults = {"team1": [], "team2": []}

    if is_tournament:
        source = tournament_record if tournament_record is not None else match_record
        team1 = get_tournament_team_by_id(source, map_entry.get("team1_tournament_team_id"))
        team2 = get_tournament_team_by_id(source, map_entry.get("team2_tournament_team_id"))
        defaults["team1"] = build_comp_slot_player_order(
            [{"name": str(name).strip(), "role": ""} for name in (team1 or {}).get("players", []) if str(name).strip()],
            slot_count=6,
        )
        defaults["team2"] = build_comp_slot_player_order(
            [{"name": str(name).strip(), "role": ""} for name in (team2 or {}).get("players", []) if str(name).strip()],
            slot_count=6,
        )
        return defaults

    db = get_db()
    our_team_id = match_record.get("team1_id") or match_record.get("team_id")
    enemy_team_id = match_record.get("team2_id") or match_record.get("enemy_team_id")
    our_team_name = (match_record.get("team1_name") or match_record.get("team_name") or "").strip().lower()
    enemy_team_name = (match_record.get("team2_name") or match_record.get("enemy_team") or match_record.get("opponent") or "").strip().lower()

    def _query_main_team_players(team_id_value: int | None = None, team_name_value: str = "") -> list[dict]:
        rows = []
        if team_id_value:
            rows = db.execute(
                "SELECT name, role FROM players WHERE team_id = ? AND COALESCE(is_sub, 0) = 0 ORDER BY name COLLATE NOCASE",
                (team_id_value,),
            ).fetchall()
        if not rows and (team_name_value or "").strip():
            team_row = db.execute(
                "SELECT id FROM teams WHERE lower(name) = lower(?)",
                ((team_name_value or "").strip(),),
            ).fetchone()
            if team_row:
                rows = db.execute(
                    "SELECT name, role FROM players WHERE team_id = ? AND COALESCE(is_sub, 0) = 0 ORDER BY name COLLATE NOCASE",
                    (team_row["id"],),
                ).fetchall()
        return [
            {"name": (row["name"] or "").strip(), "role": (row["role"] or "").strip()}
            for row in rows
            if (row["name"] or "").strip()
        ]

    def _query_legacy_enemy_players(enemy_team_id_value: int | None) -> list[dict]:
        if not enemy_team_id_value:
            return []
        rows = db.execute(
            "SELECT name, role FROM enemy_players WHERE enemy_team_id = ? ORDER BY name COLLATE NOCASE",
            (enemy_team_id_value,),
        ).fetchall()
        return [
            {"name": (row["name"] or "").strip(), "role": (row["role"] or "").strip()}
            for row in rows
            if (row["name"] or "").strip()
        ]

    def _load_side_player_pool(side_team_id: int | None, side_team_name: str = "") -> list[dict]:
        normalized_side_name = (side_team_name or "").strip().lower()

        if side_team_id and our_team_id and side_team_id == our_team_id:
            return _query_main_team_players(side_team_id)

        if side_team_id and enemy_team_id and side_team_id == enemy_team_id:
            return _query_main_team_players(side_team_id) or _query_legacy_enemy_players(side_team_id)

        if normalized_side_name and our_team_name and normalized_side_name == our_team_name:
            return _query_main_team_players(our_team_id, side_team_name)

        if normalized_side_name and enemy_team_name and normalized_side_name == enemy_team_name:
            return _query_main_team_players(enemy_team_id, side_team_name) or _query_legacy_enemy_players(enemy_team_id)

        # Fallback only when side identity is unknown.
        if side_team_id:
            return _query_main_team_players(side_team_id) or _query_legacy_enemy_players(side_team_id)

        normalized_name = (side_team_name or "").strip()
        if normalized_name:
            return _query_main_team_players(None, normalized_name)

        return []

    for side in TEAM_SLOTS:
        side_team_id = map_entry.get(f"{side}_id")
        side_team_name = map_entry.get(f"{side}_name", "")
        player_rows = _load_side_player_pool(side_team_id, side_team_name)
        defaults[side] = build_comp_slot_player_order(
            player_rows,
            slot_count=6,
        )

    return defaults


def _build_player_hero_pair_history(
    team_id: int | None,
    team_name: str,
    candidate_players: list[str],
) -> tuple[dict[tuple[str, str], dict[str, int]], dict[str, int]]:
    pair_counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"count": 0, "wins": 0})
    player_totals: dict[str, int] = defaultdict(int)

    candidate_set = {name for name in candidate_players if name}
    if not candidate_set:
        return pair_counts, player_totals

    team_scrims = get_scrims_for_team(team_id, team_name)
    for scrim in team_scrims:
        for map_entry in scrim.get("maps", []):
            if not isinstance(map_entry, dict):
                continue

            our_team_slot = normalize_match_team_slot(map_entry.get("our_team_slot", "team1"))
            outcome = get_map_outcome_for_slot(map_entry, our_team_slot)

            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(our_team_slot, []):
                    if not isinstance(slot, dict):
                        continue
                    player_name = (slot.get("player") or "").strip()
                    if not player_name or player_name not in candidate_set:
                        continue
                    hero_name = canonicalize_hero_name(slot.get("hero", ""))
                    if not hero_name:
                        continue

                    pair_counts[(hero_name, player_name)]["count"] += 1
                    if outcome == "Win":
                        pair_counts[(hero_name, player_name)]["wins"] += 1
                    player_totals[player_name] += 1

    return pair_counts, player_totals


def _auto_assign_players_to_heroes_for_side(
    team_slots: list[dict],
    candidate_players: list[str],
    *,
    team_id: int | None,
    team_name: str,
) -> list[dict]:
    cleaned_candidates = []
    seen = set()
    for name in candidate_players:
        player_name = (name or "").strip()
        if player_name and player_name not in seen:
            seen.add(player_name)
            cleaned_candidates.append(player_name)

    if not team_slots or not cleaned_candidates:
        return team_slots

    pair_counts, player_totals = _build_player_hero_pair_history(team_id, team_name, cleaned_candidates)
    candidate_index = {name: idx for idx, name in enumerate(cleaned_candidates)}
    used_players: set[str] = set()

    # Preserve explicitly assigned players and avoid reassigning them.
    for slot in team_slots:
        if not isinstance(slot, dict):
            continue
        existing_player = (slot.get("player") or "").strip()
        if existing_player:
            used_players.add(existing_player)

    hero_slot_indices = []
    for idx, slot in enumerate(team_slots):
        if not isinstance(slot, dict):
            continue
        if canonicalize_hero_name(slot.get("hero", "")):
            hero_slot_indices.append(idx)

    for idx in hero_slot_indices:
        slot = team_slots[idx]
        current_player = (slot.get("player") or "").strip()
        if current_player:
            continue
        hero_name = canonicalize_hero_name(slot.get("hero", ""))
        if not hero_name:
            continue

        best_name = None
        best_score = None
        for player_name in cleaned_candidates:
            if player_name in used_players:
                continue

            stats = pair_counts.get((hero_name, player_name), {"count": 0, "wins": 0})
            score = (
                stats.get("count", 0),
                stats.get("wins", 0),
                player_totals.get(player_name, 0),
                -candidate_index.get(player_name, 999),
            )
            if best_score is None or score > best_score:
                best_score = score
                best_name = player_name

        if best_name:
            slot["player"] = best_name
            used_players.add(best_name)

    for slot in team_slots:
        if not isinstance(slot, dict):
            continue
        if (slot.get("player") or "").strip():
            continue
        next_name = next((name for name in cleaned_candidates if name not in used_players), None)
        if next_name:
            slot["player"] = next_name
            used_players.add(next_name)

    return team_slots


def auto_assign_section_players_from_heroes(
    match_record: dict,
    map_entry: dict,
    section: dict,
    *,
    is_tournament: bool,
    tournament_record: dict | None = None,
) -> None:
    defaults = get_map_side_default_players(
        match_record,
        map_entry,
        is_tournament=is_tournament,
        tournament_record=tournament_record,
    )

    for side in TEAM_SLOTS:
        side_slots = section.get(side, [])
        if not isinstance(side_slots, list):
            continue
        section[side] = _auto_assign_players_to_heroes_for_side(
            side_slots,
            defaults.get(side, []),
            team_id=map_entry.get(f"{side}_id"),
            team_name=(map_entry.get(f"{side}_name") or "").strip(),
        )


def scrim_involves_team(scrim: dict, team_id: int | None, team_name: str = "") -> bool:
    if team_id is not None and (
        scrim.get("team1_id") == team_id
        or scrim.get("team2_id") == team_id
        or scrim.get("team_id") == team_id
        or scrim.get("enemy_team_id") == team_id
    ):
        return True

    team_name_lower = (team_name or "").strip().lower()
    if not team_name_lower:
        return False

    participant_names = [
        (scrim.get("team_name", "") or "").strip().lower(),
        (scrim.get("team1_name", "") or "").strip().lower(),
        (scrim.get("team2_name", "") or "").strip().lower(),
        (scrim.get("enemy_team", "") or "").strip().lower(),
        (scrim.get("opponent", "") or "").strip().lower(),
    ]
    return any(name == team_name_lower for name in participant_names if name)


def get_scrims_for_team(team_id: int | None, team_name: str = "") -> list[dict]:
    relevant_scrims = [scrim for scrim in SCRIMS if scrim_involves_team(scrim, team_id, team_name)]
    remapped_scrims: list[dict] = []

    team_name_lower = (team_name or "").strip().lower()

    def _normalize_player_keys(raw_names: list[str]) -> set[str]:
        keys: set[str] = set()
        for raw_name in raw_names or []:
            player_name = normalize_player_name(raw_name)
            key = _compact_text(player_name)
            if key:
                keys.add(key)
        return keys

    selected_team_player_keys: set[str] = set()
    if team_id is not None or team_name_lower:
        roster_db = get_db() if has_request_context() else _connect_db()
        try:
            resolved_team_id = team_id
            if resolved_team_id is None and team_name_lower:
                team_row = roster_db.execute(
                    "SELECT id FROM teams WHERE lower(name) = lower(?)",
                    (team_name,),
                ).fetchone()
                if team_row is not None:
                    resolved_team_id = int(team_row["id"])
            if resolved_team_id is not None:
                selected_team_player_keys = _normalize_player_keys(
                    [
                        row["name"]
                        for row in roster_db.execute(
                            "SELECT name FROM players WHERE team_id = ? AND COALESCE(is_sub, 0) = 0",
                            (resolved_team_id,),
                        ).fetchall()
                    ]
                )
        finally:
            if not has_request_context():
                roster_db.close()

    def _resolve_slot_for_record(record: dict, fallback_slot: str | None = None) -> str:
        if team_id is not None:
            if record.get("team1_id") == team_id:
                return "team1"
            if record.get("team2_id") == team_id:
                return "team2"
            if record.get("team_id") == team_id:
                return normalize_match_team_slot(record.get("team_slot", "team1"))
            if record.get("enemy_team_id") == team_id:
                return opposite_team_slot(normalize_match_team_slot(record.get("team_slot", "team1")))

        if team_name_lower:
            if str(record.get("team1_name", "")).strip().lower() == team_name_lower:
                return "team1"
            if str(record.get("team2_name", "")).strip().lower() == team_name_lower:
                return "team2"
            if str(record.get("team_name", "")).strip().lower() == team_name_lower:
                return normalize_match_team_slot(record.get("team_slot", "team1"))
            if (
                str(record.get("enemy_team", "")).strip().lower() == team_name_lower
                or str(record.get("opponent", "")).strip().lower() == team_name_lower
            ):
                return opposite_team_slot(normalize_match_team_slot(record.get("team_slot", "team1")))

        return normalize_match_team_slot(fallback_slot or record.get("team_slot", "team1"))

    def _map_side_player_keys(map_record: dict, side: str) -> set[str]:
        keys: set[str] = set()
        for section in map_record.get("comp", []):
            if not isinstance(section, dict):
                continue
            for slot in section.get(side, []):
                if not isinstance(slot, dict):
                    continue
                player_name = normalize_player_name(slot.get("player", ""))
                key = _compact_text(player_name)
                if key:
                    keys.add(key)
        return keys

    def _resolve_map_team_slot(scrim_record: dict, map_record: dict, scrim_slot: str) -> str:
        stored_map_slot = (map_record.get("our_team_slot") or "").strip()
        if (
            team_id is not None
            and scrim_record.get("team_id") == team_id
            and stored_map_slot in TEAM_SLOTS
        ):
            map_team_slot = stored_map_slot
        else:
            map_team_slot = _resolve_slot_for_record(map_record, scrim_slot)

        roster_candidates: list[set[str]] = []
        scrim_side_player_keys = _normalize_player_keys(scrim_record.get(f"{scrim_slot}_players", []))
        if scrim_side_player_keys:
            roster_candidates.append(scrim_side_player_keys)
        if selected_team_player_keys:
            roster_candidates.append(selected_team_player_keys)
        if not roster_candidates:
            return map_team_slot

        current_side_keys = _map_side_player_keys(map_record, map_team_slot)
        other_team_slot = opposite_team_slot(map_team_slot)
        other_side_keys = _map_side_player_keys(map_record, other_team_slot)
        if not current_side_keys and not other_side_keys:
            return map_team_slot

        current_score = max(len(current_side_keys & roster_keys) for roster_keys in roster_candidates)
        other_score = max(len(other_side_keys & roster_keys) for roster_keys in roster_candidates)
        return other_team_slot if other_score > current_score else map_team_slot

    for original_scrim in relevant_scrims:
        scrim = copy.deepcopy(original_scrim)
        team_slot = _resolve_slot_for_record(scrim)

        remapped_maps: list[dict] = []
        for original_map in scrim.get("maps", []):
            if not isinstance(original_map, dict):
                continue
            map_entry = copy.deepcopy(original_map)
            map_team_slot = _resolve_map_team_slot(original_scrim, original_map, team_slot)
            map_entry["our_team_slot"] = map_team_slot
            map_entry["result"] = get_result_for_slot(original_map, map_team_slot)
            inferred_result = infer_result_from_score_text(map_entry.get("score", ""), slot=map_team_slot)
            if inferred_result:
                map_entry["result"] = inferred_result
            remapped_maps.append(map_entry)

        scrim["team_slot"] = team_slot
        scrim["maps"] = remapped_maps
        remapped_scrims.append(scrim)

    return remapped_scrims


def get_team_history_scrims(team_row: sqlite3.Row | dict) -> list[dict]:
    """Return scrim + tournament scrim history for a team row."""
    team_id = int(team_row["id"])
    team_name = (team_row["name"] or "").strip()
    scrims = get_scrims_for_team(team_id, team_name)
    tournament_scrims = build_team_tournament_scrims(team_row)
    return scrims + tournament_scrims


def get_team_history_for_sources(
    team_row: sqlite3.Row | dict,
    *,
    include_scrims: bool = True,
    include_tournaments: bool = True,
) -> list[dict]:
    """Return team history filtered by the machine source toggles."""
    team_id = int(team_row["id"])
    team_name = (team_row["name"] or "").strip()
    history: list[dict] = []
    if include_scrims:
        history.extend(get_scrims_for_team(team_id, team_name))
    if include_tournaments:
        history.extend(build_team_tournament_scrims(team_row))
    return history


def _bool_arg(name: str, default: bool = True) -> bool:
    raw_value = request.args.get(name)
    if raw_value is None:
        return default
    return str(raw_value).strip().lower() not in {"0", "false", "off", "no"}


