# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def canonicalize_hero_name(raw_hero: str) -> str:
    return normalize_hero_slot_value(raw_hero)


def is_map_draft_mirrored(map_entry: dict) -> bool:
    draft = map_entry.get("draft", {})
    if not isinstance(draft, dict):
        return False

    team1_draft = draft.get("team1", {}) if isinstance(draft.get("team1", {}), dict) else {}
    team2_draft = draft.get("team2", {}) if isinstance(draft.get("team2", {}), dict) else {}
    team1_heroes = {
        canonicalize_hero_name(hero)
        for hero in team1_draft.values()
        if canonicalize_hero_name(hero)
    }
    team2_heroes = {
        canonicalize_hero_name(hero)
        for hero in team2_draft.values()
        if canonicalize_hero_name(hero)
    }
    if not team1_heroes or not team2_heroes:
        return False
    return len(team1_heroes & team2_heroes) >= 3


def is_map_draft_unmirrored(map_entry: dict) -> bool:
    """Check if a map has unmirrored draft: 1-2 shared heroes between teams (some overlap, but not fully mirrored)."""
    draft = map_entry.get("draft", {})
    if not isinstance(draft, dict):
        return False

    team1_draft = draft.get("team1", {}) if isinstance(draft.get("team1", {}), dict) else {}
    team2_draft = draft.get("team2", {}) if isinstance(draft.get("team2", {}), dict) else {}
    team1_heroes = {
        canonicalize_hero_name(hero)
        for hero in team1_draft.values()
        if canonicalize_hero_name(hero)
    }
    team2_heroes = {
        canonicalize_hero_name(hero)
        for hero in team2_draft.values()
        if canonicalize_hero_name(hero)
    }
    if not team1_heroes or not team2_heroes:
        return False
    shared_count = len(team1_heroes & team2_heroes)
    # Unmirrored: 1-2 shared heroes (not fully mirrored with 3+, but not completely different with 0)
    return 1 <= shared_count <= 2


def normalize_player_role(raw_role: str) -> str:
    candidate = raw_role.strip().lower()
    for role in PLAYER_ROLES:
        if candidate == role.lower():
            return role
    return ""


STAFF_ROLES = {"Coach", "AC", "Analyst"}


def is_staff_role(raw_role: str | None) -> bool:
    return (raw_role or "").strip() in STAFF_ROLES


def build_comp_slot_player_order(player_pool: list[dict], slot_count: int = 6) -> list[str]:
    cleaned: list[dict] = []
    seen_names: set[str] = set()
    for row in player_pool:
        name = str((row or {}).get("name", "")).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        cleaned.append({
            "name": name,
            "role": normalize_player_role(str((row or {}).get("role", ""))),
        })

    desired_roles = ["Vanguard", "Vanguard", "Duelist", "Duelist", "Strategist", "Strategist"]
    if slot_count > len(desired_roles):
        desired_roles.extend([""] * (slot_count - len(desired_roles)))

    buckets = {
        "Vanguard": [row["name"] for row in cleaned if row["role"] == "Vanguard"],
        "Duelist": [row["name"] for row in cleaned if row["role"] == "Duelist"],
        "Strategist": [row["name"] for row in cleaned if row["role"] == "Strategist"],
        "Flex": [row["name"] for row in cleaned if row["role"] == "Flex"],
        "": [row["name"] for row in cleaned if not row["role"]],
    }

    selected: list[str] = []
    selected_keys: set[str] = set()

    def _take_from(role_key: str) -> str:
        queue = buckets.get(role_key, [])
        while queue:
            candidate = queue.pop(0)
            candidate_key = candidate.lower()
            if candidate_key in selected_keys:
                continue
            selected_keys.add(candidate_key)
            return candidate
        return ""

    def _take_any_remaining() -> str:
        for role_key in ("Vanguard", "Duelist", "Strategist", "Flex", ""):
            candidate = _take_from(role_key)
            if candidate:
                return candidate
        return ""

    for desired_role in desired_roles[:slot_count]:
        candidate = _take_from(desired_role) if desired_role else ""
        if not candidate:
            candidate = _take_any_remaining()
        if candidate:
            selected.append(candidate)

    return selected


def team_has_duplicate_heroes(team_slots: list[dict]) -> bool:
    seen: set[str] = set()
    for slot in team_slots:
        hero = str((slot or {}).get("hero", "")).strip()
        if not hero:
            continue
        hero_key = hero.lower()
        if hero_key in seen:
            return True
        seen.add(hero_key)
    return False


def parse_team_id(raw_team_id: str) -> int | None:
    value = (raw_team_id or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    return None


def get_team_name_by_id(team_id: int | None) -> str:
    if team_id is None:
        return ""
    row = get_db().execute("SELECT name FROM teams WHERE id = ?", (team_id,)).fetchone()
    return row["name"] if row else ""


def get_enemy_team_name_by_id(team_id: int | None, enemy_team_id: int | None) -> str:
    if team_id is None or enemy_team_id is None:
        return ""
    row = get_db().execute(
        "SELECT name FROM enemy_teams WHERE id = ? AND team_id = ?",
        (enemy_team_id, team_id),
    ).fetchone()
    return row["name"] if row else ""


def migrate_enemy_teams_to_team_database(db: sqlite3.Connection) -> int:
    """Move legacy enemy-team records into the main team database tables."""
    enemy_rows = db.execute(
        "SELECT id, name, notes, logo_path FROM enemy_teams ORDER BY id"
    ).fetchall()
    if not enemy_rows:
        return 0

    moved_count = 0
    migrated_enemy_ids: list[int] = []

    for enemy_row in enemy_rows:
        enemy_name = (enemy_row["name"] or "").strip()
        if not enemy_name:
            continue

        target_row = db.execute(
            "SELECT id FROM teams WHERE lower(name) = lower(?)",
            (enemy_name,),
        ).fetchone()
        if target_row is None:
            try:
                db.execute(
                    "INSERT INTO teams (name, notes, logo_path, is_personal) VALUES (?, ?, ?, 0)",
                    (enemy_name, enemy_row["notes"] or "", enemy_row["logo_path"] or ""),
                )
            except sqlite3.IntegrityError:
                pass
            target_row = db.execute(
                "SELECT id FROM teams WHERE lower(name) = lower(?)",
                (enemy_name,),
            ).fetchone()

        if target_row is None:
            continue

        target_team_id = target_row["id"]
        player_rows = db.execute(
            "SELECT name, role, main_hero, notes FROM enemy_players WHERE enemy_team_id = ?",
            (enemy_row["id"],),
        ).fetchall()
        for player_row in player_rows:
            player_name = (player_row["name"] or "").strip()
            if not player_name:
                continue
            try:
                db.execute(
                    """
                    INSERT INTO players (team_id, name, role, main_hero, notes)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        target_team_id,
                        player_name,
                        player_row["role"] or "",
                        player_row["main_hero"] or "",
                        player_row["notes"] or "",
                    ),
                )
            except sqlite3.IntegrityError:
                continue

        migrated_enemy_ids.append(enemy_row["id"])
        moved_count += 1

    if migrated_enemy_ids:
        db.executemany("DELETE FROM enemy_teams WHERE id = ?", [(enemy_id,) for enemy_id in migrated_enemy_ids])
        db.commit()

    return moved_count


def build_match_map_entry_from_form() -> dict:
    global NEXT_MAP_ID

    map_name = request.form.get("map_name", "").strip()
    map_type = normalize_map_type_value(request.form.get("map_type", ""))
    result = request.form.get("result", "").strip()
    if result not in RESULTS:
        result = ""

    our_team_slot = request.form.get("our_team_slot", "team1").strip()
    if our_team_slot not in TEAM_SLOTS:
        our_team_slot = "team1"

    draft = {
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

    first_submap = request.form.get("first_submap", "").strip()
    map_entry = {
        "id": NEXT_MAP_ID,
        "map_name": map_name,
        "map_type": map_type,
        "side": "",
        "our_team_slot": our_team_slot,
        "result": result,
        "score": request.form.get("score", "").strip(),
        "draft": draft,
        "comp": build_default_comp_sections(map_name, first_submap=first_submap),
        "notes": request.form.get("notes", "").strip(),
        "vod_url": "",
        "events": [],
    }
    NEXT_MAP_ID += 1
    return map_entry


def build_match_map_detail_context(match_record: dict, map_entry: dict, *, is_tournament: bool, tournament_record: dict | None = None) -> dict:
    if map_entry.get("our_team_slot") not in TEAM_SLOTS:
        map_entry["our_team_slot"] = "team1"
    if map_entry.get("map_type") not in MAP_TYPES:
        map_entry["map_type"] = DEFAULT_MAP_TYPE

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
        if side_value not in SIDES:
            side_value = ""
        sec["side"] = side_value

    team_players = []
    enemy_players = []
    team1_player_options: list[dict] = []
    team2_player_options: list[dict] = []
    team1_default_players: list[str] = []
    team2_default_players: list[str] = []
    enemy_team_data = None
    team1_label = match_record.get("team_name") or match_record.get("team1_name") or "Team 1"
    team2_label = match_record.get("enemy_team") or match_record.get("opponent") or match_record.get("team2_name") or "Team 2"
    participant_one_id = None
    participant_two_id = None
    participant_one_label = ""
    participant_two_label = ""
    picked_by_label = ""

    db = get_db()

    def _build_default_player_slots(player_options: list[dict]) -> list[str]:
        main_pool = [
            {
                "name": (option.get("name") or "").strip(),
                "role": (option.get("role") or "").strip(),
            }
            for option in player_options
            if (option.get("name") or "").strip() and not bool(option.get("is_sub"))
        ]
        return build_comp_slot_player_order(main_pool, slot_count=6)

    if is_tournament:
        tournament_source = tournament_record if tournament_record is not None else match_record
        team1 = get_tournament_team_by_id(tournament_source, map_entry.get("team1_tournament_team_id"))
        team2 = get_tournament_team_by_id(tournament_source, map_entry.get("team2_tournament_team_id"))
        picker = get_tournament_team_by_id(tournament_source, map_entry.get("picked_by_tournament_team_id"))
        team1_label = (team1 or {}).get("name") or map_entry.get("team1_name") or "Team 1"
        team2_label = (team2 or {}).get("name") or map_entry.get("team2_name") or "Team 2"
        map_entry["team1_name"] = team1_label
        map_entry["team2_name"] = team2_label
        map_entry["picked_by_name"] = (picker or {}).get("name") or str(map_entry.get("picked_by_name", "")).strip()
        picked_by_label = map_entry.get("picked_by_name", "")
        team_players = list((team1 or {}).get("players", []))
        if not team_players and team1_label:
            team_row = db.execute(
                "SELECT id FROM teams WHERE lower(name) = lower(?)",
                (team1_label,),
            ).fetchone()
            if team_row:
                team_players = [
                    (row["name"] or "").strip()
                    for row in db.execute(
                        "SELECT name, role FROM players WHERE team_id = ? AND COALESCE(is_sub, 0) = 0 ORDER BY name COLLATE NOCASE",
                        (team_row["id"],),
                    ).fetchall()
                    if (row["name"] or "").strip() and not is_staff_role(row["role"])
                ]
        team2_players = list((team2 or {}).get("players", []))
        if not team2_players and team2_label:
            team_row = db.execute(
                "SELECT id FROM teams WHERE lower(name) = lower(?)",
                (team2_label,),
            ).fetchone()
            if team_row:
                team2_players = [
                    (row["name"] or "").strip()
                    for row in db.execute(
                        "SELECT name, role FROM players WHERE team_id = ? AND COALESCE(is_sub, 0) = 0 ORDER BY name COLLATE NOCASE",
                        (team_row["id"],),
                    ).fetchall()
                    if (row["name"] or "").strip() and not is_staff_role(row["role"])
                ]
        enemy_players = [
            {
                "name": player_name,
                "role": "",
                "main_hero": "",
            }
            for player_name in team2_players
        ]
        team1_player_options = [{"name": player_name, "role": "", "is_sub": False} for player_name in team_players]
        team2_player_options = [{"name": player_name, "role": "", "is_sub": False} for player_name in team2_players]
    else:
        participant_one, participant_two = get_scrim_participants(match_record)
        participant_one_label, participant_two_label = get_scrim_participant_labels(match_record)
        participant_one_id = participant_one.get("id")
        participant_two_id = participant_two.get("id")
        if not map_entry.get("team1_id") and participant_one_id:
            map_entry["team1_id"] = participant_one_id
        if not map_entry.get("team2_id") and participant_two_id:
            map_entry["team2_id"] = participant_two_id
        if not (map_entry.get("team1_name") or "").strip():
            map_entry["team1_name"] = participant_one_label
        if not (map_entry.get("team2_name") or "").strip():
            map_entry["team2_name"] = participant_two_label
        team1_label = (map_entry.get("team1_name") or "").strip() or participant_one_label
        team2_label = (map_entry.get("team2_name") or "").strip() or participant_two_label

        # Canonicalize side IDs by team names so each side resolves to the
        # correct roster even when legacy ids drift after migrations.
        team1_row = db.execute(
            "SELECT id FROM teams WHERE lower(name) = lower(?)",
            (team1_label,),
        ).fetchone() if team1_label else None
        team2_row = db.execute(
            "SELECT id FROM teams WHERE lower(name) = lower(?)",
            (team2_label,),
        ).fetchone() if team2_label else None

        if team1_row:
            map_entry["team1_id"] = team1_row["id"]
        if team2_row:
            map_entry["team2_id"] = team2_row["id"]

        # If labels are different, never allow both sides to share the same id.
        if (
            (team1_label or "").strip().lower() != (team2_label or "").strip().lower()
            and map_entry.get("team1_id")
            and map_entry.get("team1_id") == map_entry.get("team2_id")
        ):
            if team2_row:
                map_entry["team2_id"] = team2_row["id"]
            elif team1_row:
                map_entry["team1_id"] = team1_row["id"]

        team_id = match_record.get("team_id")
        player_rows = []

        def _load_team_player_options(team_id_value: int | None) -> list[dict]:
            if not team_id_value:
                return []
            rows = db.execute(
                "SELECT name, role, is_sub FROM players WHERE team_id = ? ORDER BY is_sub ASC, name COLLATE NOCASE",
                (team_id_value,),
            ).fetchall()
            return [
                {
                    "name": (row["name"] or "").strip(),
                    "role": (row["role"] or "").strip(),
                    "is_sub": bool(row["is_sub"]),
                }
                for row in rows
                if (row["name"] or "").strip() and not is_staff_role(row["role"])
            ]

        def _load_enemy_player_options(enemy_team_id_value: int | None) -> list[dict]:
            if not enemy_team_id_value:
                return []
            rows = db.execute(
                "SELECT name, role, is_sub FROM players WHERE team_id = ? ORDER BY is_sub ASC, name COLLATE NOCASE",
                (enemy_team_id_value,),
            ).fetchall()
            if not rows:
                rows = db.execute(
                    "SELECT name, role, 0 as is_sub FROM enemy_players WHERE enemy_team_id = ? ORDER BY name COLLATE NOCASE",
                    (enemy_team_id_value,),
                ).fetchall()
            return [
                {
                    "name": (row["name"] or "").strip(),
                    "role": (row["role"] or "").strip(),
                    "is_sub": bool(row["is_sub"]),
                }
                for row in rows
                if (row["name"] or "").strip() and not is_staff_role(row["role"])
            ]

        if team_id:
            player_options = _load_team_player_options(team_id)
            player_rows = player_options
            team_players = [row["name"] for row in player_options]

        enemy_team_id = map_entry.get("team2_id") or match_record.get("team2_id") or match_record.get("enemy_team_id")
        if enemy_team_id:
            enemy_team_rows = db.execute(
                "SELECT id, name, notes FROM teams WHERE id = ?",
                (enemy_team_id,),
            ).fetchone()
            if enemy_team_rows:
                enemy_team_data = dict(enemy_team_rows)
                enemy_player_rows = db.execute(
                    "SELECT name, role, main_hero FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
                    (enemy_team_id,),
                ).fetchall()
                enemy_players = [dict(row) for row in enemy_player_rows if not is_staff_role(row["role"])]

        our_team_id = map_entry.get("team1_id") or match_record.get("team1_id") or match_record.get("team_id")
        enemy_team_id = map_entry.get("team2_id") or match_record.get("team2_id") or match_record.get("enemy_team_id")
        our_team_name = (map_entry.get("team1_name") or match_record.get("team1_name") or match_record.get("team_name") or "").strip().lower()
        enemy_team_name = (map_entry.get("team2_name") or match_record.get("team2_name") or match_record.get("enemy_team") or match_record.get("opponent") or "").strip().lower()

        our_player_options = [dict(row) for row in player_rows]
        if not our_player_options:
            our_player_options = [{"name": player_name, "role": "", "is_sub": False} for player_name in team_players]

        known_enemy_player_options = _load_enemy_player_options(enemy_team_id)
        if not known_enemy_player_options:
            known_enemy_player_options = [
                {
                    "name": (player.get("name") or "").strip(),
                    "role": (player.get("role") or "").strip(),
                    "is_sub": False,
                }
                for player in enemy_players
                if (player.get("name") or "").strip() and not is_staff_role(player.get("role"))
            ]

        side_options_cache: dict[tuple[int | None, str], list[dict]] = {}

        def _load_side_options(side_team_id: int | None, side_team_name: str) -> list[dict]:
            cache_key = (side_team_id, (side_team_name or "").strip().lower())
            if cache_key in side_options_cache:
                return list(side_options_cache[cache_key])

            resolved: list[dict] = []
            normalized_side_name = (side_team_name or "").strip().lower()

            if side_team_id and our_team_id and side_team_id == our_team_id:
                resolved = _load_team_player_options(our_team_id)
            elif side_team_id and enemy_team_id and side_team_id == enemy_team_id:
                resolved = _load_enemy_player_options(enemy_team_id)
            elif normalized_side_name and our_team_name and normalized_side_name == our_team_name:
                resolved = _load_team_player_options(our_team_id)
            elif normalized_side_name and enemy_team_name and normalized_side_name == enemy_team_name:
                resolved = _load_enemy_player_options(enemy_team_id)

            if not resolved and side_team_id:
                resolved = _load_team_player_options(side_team_id)
                if not resolved:
                    resolved = _load_enemy_player_options(side_team_id)

            if not resolved and side_team_name:
                team_row = db.execute(
                    "SELECT id FROM teams WHERE lower(name) = lower(?)",
                    ((side_team_name or "").strip(),),
                ).fetchone()
                if team_row:
                    resolved = _load_team_player_options(team_row["id"])

            side_options_cache[cache_key] = list(resolved)
            return list(resolved)

        def _resolve_side_player_options(side_team_id: int | None, side_team_name: str) -> list[dict]:
            direct_match = _load_side_options(side_team_id, side_team_name)
            if direct_match:
                return direct_match

            if side_team_id and our_team_id and side_team_id == our_team_id:
                return list(our_player_options)
            if side_team_id and enemy_team_id and side_team_id == enemy_team_id:
                return list(known_enemy_player_options)

            side_name = (side_team_name or "").strip().lower()
            if side_name and our_team_name and side_name == our_team_name:
                return list(our_player_options)
            if side_name and enemy_team_name and side_name == enemy_team_name:
                return list(known_enemy_player_options)
            return []

        team1_player_options = _resolve_side_player_options(map_entry.get("team1_id"), map_entry.get("team1_name", ""))
        team2_player_options = _resolve_side_player_options(map_entry.get("team2_id"), map_entry.get("team2_name", ""))

    def _extract_comp_players(slot_key: str) -> list[dict]:
        seen: set[str] = set()
        extracted: list[dict] = []
        for section in map_entry.get("comp", []):
            if not isinstance(section, dict):
                continue
            for slot in section.get(slot_key, []):
                if not isinstance(slot, dict):
                    continue
                player_name = (slot.get("player") or "").strip()
                if not player_name:
                    continue
                normalized = player_name.lower()
                if normalized in seen:
                    continue
                seen.add(normalized)
                extracted.append({"name": player_name, "role": "", "is_sub": False})
        return extracted

    if not team1_player_options:
        team1_player_options = _extract_comp_players("team1")
    if not team2_player_options:
        team2_player_options = _extract_comp_players("team2")

    team1_default_players = _build_default_player_slots(team1_player_options)
    team2_default_players = _build_default_player_slots(team2_player_options)

    map_draft_timeline_row = None
    target_map_name = (map_entry.get("map_name") or "").strip()
    if target_map_name:
        source_scrims: list[dict] = []
        if is_tournament:
            perspective = map_entry.get("our_team_slot", "team1") if map_entry.get("our_team_slot", "team1") in TEAM_SLOTS else "team1"
            if tournament_record is not None:
                source_scrims = build_tournament_match_scrims(tournament_record, perspective=perspective)
        else:
            team_id = match_record.get("team_id")
            team_name = (match_record.get("team_name") or match_record.get("team1_name") or "").strip()
            if team_id and team_name:
                source_scrims = get_scrims_for_team(team_id, team_name)

        filtered_scrims: list[dict] = []
        for scrim in source_scrims:
            matching_maps = [m for m in scrim.get("maps", []) if (m.get("map_name") or "").strip() == target_map_name]
            if not matching_maps:
                continue
            scrim_copy = copy.deepcopy(scrim)
            scrim_copy["maps"] = matching_maps
            filtered_scrims.append(scrim_copy)

        if filtered_scrims:
            map_timeline = build_draft_phase_timeline(filtered_scrims)
            map_draft_timeline_row = next(
                (row for row in map_timeline.get("maps", []) if row.get("map_name") == target_map_name),
                None,
            )

    return {
        "match_record": match_record,
        "map_entry": map_entry,
        "heroes": HEROES,
        "hero_roles": HERO_ROLES,
        "hero_transformations": HERO_TRANSFORMATIONS,
        "map_images": MAP_IMAGES,
        "map_submaps": MAP_SUBMAPS,
        "map_mode": MAP_MODES.get(map_entry.get("map_name", ""), "Other"),
        "maps": MAPS,
        "map_types": MAP_TYPES,
        "sides": SIDES,
        "results": RESULTS,
        "event_types": EVENT_TYPES,
        "team_players": team_players,
        "enemy_team": enemy_team_data,
        "enemy_players": enemy_players,
        "team1_player_options": team1_player_options,
        "team2_player_options": team2_player_options,
        "team1_default_players": team1_default_players,
        "team2_default_players": team2_default_players,
        "team1_label": team1_label,
        "team2_label": team2_label,
        "participant_one_id": participant_one_id,
        "participant_two_id": participant_two_id,
        "participant_one_label": participant_one_label,
        "participant_two_label": participant_two_label,
        "picked_by_label": picked_by_label,
        "map_draft_timeline_row": map_draft_timeline_row,
        "split_score_pair": split_score_pair,
    }


