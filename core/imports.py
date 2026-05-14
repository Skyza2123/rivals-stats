# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def get_scrim_or_404(scrim_id: int) -> dict:
    for scrim in SCRIMS:
        if scrim["id"] == scrim_id:
            return scrim
    abort(404)


def get_tournament_or_404(tournament_id: int) -> dict:
    for match in TOURNAMENT_MATCHES:
        if match["id"] == tournament_id:
            return match
    abort(404)


def get_tournament_match_or_404(tournament_record: dict, match_id: int) -> dict:
    for tournament_match in tournament_record.get("matches", []):
        if isinstance(tournament_match, dict) and tournament_match.get("id") == match_id:
            return tournament_match
    abort(404)


def get_map_or_404(scrim: dict, map_id: int) -> dict:
    for map_entry in scrim["maps"]:
        if map_entry["id"] == map_id:
            return map_entry
    abort(404)


def parse_comma_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def is_ringer_player_name(raw_name: str | None) -> bool:
    name = str(raw_name or "").strip()
    if not name:
        return False

    lowered = name.lower()
    if re.match(r"^(?:r|ringer|sub|substitute|stand[\s-]?in|merc(?:enary)?)\s*(?::|\s*-\s+)", lowered):
        return True
    if re.search(r"[\[(](?:r|ringer|sub|substitute|stand[\s-]?in|merc(?:enary)?)[\])]\s*$", lowered):
        return True

    compact = _compact_text(name)
    return compact in _RINGER_NAME_MARKER_KEYS


def normalize_player_name(raw_name: str | None) -> str:
    name = str(raw_name or "").strip()
    if not name:
        return ""

    name = re.sub(r"\s+", " ", name)
    name = name.strip("`\"'")
    name = re.sub(r"^(?:r|ringer|sub|substitute|stand[\s-]?in|merc(?:enary)?)\s*(?::\s*|\s*-\s+)", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*[\[(](?:r|ringer|sub|substitute|stand[\s-]?in|merc(?:enary)?)[\])]\s*$", "", name, flags=re.IGNORECASE)
    name = name.strip()

    if not name:
        return ""

    alias = PLAYER_NAME_ALIASES.get(_compact_text(name))
    return alias or name


def parse_name_list(raw: str) -> list[str]:
    parts = re.split(r"[\r\n,]+", raw or "")
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        original = part.strip()
        if not original or is_ringer_player_name(original):
            continue
        name = normalize_player_name(original)
        if not name:
            continue
        key = _compact_text(name) or name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    return cleaned


def upsert_team_and_players(
    team_name: str,
    player_names: list[str],
    player_main_heroes: dict[str, str] | None = None,
) -> int | None:
    normalized_team_name = str(team_name or "").strip()
    if not normalized_team_name:
        return None

    db = get_db()
    team_row = db.execute(
        "SELECT id FROM teams WHERE lower(name) = lower(?)",
        (normalized_team_name,),
    ).fetchone()

    created_or_updated = False
    if team_row is None:
        db.execute(
            "INSERT INTO teams (name, notes, logo_path, is_personal) VALUES (?, '', '', 0)",
            (normalized_team_name,),
        )
        team_row = db.execute(
            "SELECT id FROM teams WHERE lower(name) = lower(?)",
            (normalized_team_name,),
        ).fetchone()
        created_or_updated = True

    if team_row is None:
        return None

    team_id = int(team_row["id"])
    deduped_players = parse_name_list("\n".join(player_names or []))
    normalized_player_mains = {
        str(player_name).strip().lower(): normalize_hero_slot_value(hero_name)
        for player_name, hero_name in (player_main_heroes or {}).items()
        if str(player_name).strip() and normalize_hero_slot_value(hero_name)
    }
    for player_name in deduped_players:
        player_main_hero = normalized_player_mains.get(player_name.lower(), "")
        try:
            db.execute(
                """
                INSERT INTO players (team_id, name, role, is_sub, main_hero, notes)
                VALUES (?, ?, '', 0, '', '')
                """,
                (team_id, player_name),
            )
            created_or_updated = True
        except sqlite3.IntegrityError:
            pass

        if player_main_hero:
            updated_row = db.execute(
                """
                UPDATE players
                SET main_hero = ?
                WHERE team_id = ?
                  AND lower(name) = lower(?)
                  AND trim(coalesce(main_hero, '')) = ''
                """,
                (player_main_hero, team_id, player_name),
            )
            if updated_row.rowcount:
                created_or_updated = True

    if created_or_updated:
        db.commit()

    return team_id


def _resolve_import_enemy_team_id(enemy_name: str, enemy_lookup: dict[str, int]) -> int | None:
    for enemy_key in _team_name_match_keys(enemy_name):
        if enemy_key in enemy_lookup:
            return enemy_lookup[enemy_key]
    return None


def _collect_scrim_roster_data(scrim: dict) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    players_by_side: dict[str, list[str]] = {
        "team1": list(scrim.get("team1_players", [])),
        "team2": list(scrim.get("team2_players", [])),
    }
    seen_names: dict[str, set[str]] = {
        side: {name.lower() for name in players_by_side.get(side, []) if name}
        for side in TEAM_SLOTS
    }
    hero_counts: dict[str, dict[str, Counter[str]]] = {
        "team1": defaultdict(Counter),
        "team2": defaultdict(Counter),
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
                for slot in slots:
                    if not isinstance(slot, dict):
                        continue
                    raw_player_name = str(slot.get("player", "")).strip()
                    if is_ringer_player_name(raw_player_name):
                        continue
                    player_name = normalize_player_name(raw_player_name)
                    hero_name = canonicalize_hero_name(slot.get("hero", ""))
                    if player_name:
                        player_key = _compact_text(player_name) or player_name.lower()
                        if player_key not in seen_names[side]:
                            players_by_side[side].append(player_name)
                            seen_names[side].add(player_key)
                    if player_name and hero_name:
                        hero_counts[side][player_name][hero_name] += 1

    player_main_heroes: dict[str, dict[str, str]] = {"team1": {}, "team2": {}}
    for side in TEAM_SLOTS:
        players_by_side[side] = parse_name_list("\n".join(players_by_side[side]))
        for player_name, counts in hero_counts[side].items():
            if not counts:
                continue
            best_hero = max(counts.items(), key=lambda item: (item[1], item[0]))[0]
            if best_hero:
                player_main_heroes[side][player_name] = best_hero

    return players_by_side, player_main_heroes


def _prepare_imported_scrim_context(
    scrim: dict,
    selected_team_id: int,
    selected_team_name: str,
    enemy_lookup: dict[str, int],
) -> None:
    first_map = next((map_entry for map_entry in scrim.get("maps", []) if isinstance(map_entry, dict)), None)
    selected_name = str(selected_team_name or "").strip()
    our_slot = normalize_match_team_slot((first_map or {}).get("our_team_slot", scrim.get("team_slot", "team1")))
    team1_name = str((first_map or {}).get("team1_name") or scrim.get("team1_name") or "").strip()
    team2_name = str((first_map or {}).get("team2_name") or scrim.get("team2_name") or "").strip()

    if our_slot == "team1":
        enemy_name = team2_name or str(scrim.get("enemy_team") or scrim.get("opponent") or "").strip()
        enemy_id = _resolve_import_enemy_team_id(enemy_name, enemy_lookup)
        canonical_enemy_name = get_team_name_by_id(enemy_id) if enemy_id else enemy_name
        scrim.update(
            {
                "team_slot": "team1",
                "team_id": selected_team_id,
                "team_name": selected_name,
                "team1_id": selected_team_id,
                "team1_name": selected_name or team1_name,
                "team2_id": enemy_id,
                "team2_name": canonical_enemy_name,
                "enemy_team_id": enemy_id,
                "enemy_team": canonical_enemy_name,
                "opponent": canonical_enemy_name,
            }
        )
        return

    enemy_name = team1_name or str(scrim.get("enemy_team") or scrim.get("opponent") or "").strip()
    enemy_id = _resolve_import_enemy_team_id(enemy_name, enemy_lookup)
    canonical_enemy_name = get_team_name_by_id(enemy_id) if enemy_id else enemy_name
    scrim.update(
        {
            "team_slot": "team2",
            "team_id": selected_team_id,
            "team_name": selected_name,
            "team1_id": enemy_id,
            "team1_name": canonical_enemy_name,
            "team2_id": selected_team_id,
            "team2_name": selected_name or team2_name,
            "enemy_team_id": enemy_id,
            "enemy_team": canonical_enemy_name,
            "opponent": canonical_enemy_name,
        }
    )


def _sync_scrim_rosters_with_database(scrim: dict) -> None:
    players_by_side, player_main_heroes = _collect_scrim_roster_data(scrim)
    scrim["team1_players"] = players_by_side["team1"]
    scrim["team2_players"] = players_by_side["team2"]

    team1_name = str(scrim.get("team1_name", "")).strip()
    team2_name = str(scrim.get("team2_name", "")).strip()
    team1_id = upsert_team_and_players(team1_name, players_by_side["team1"], player_main_heroes["team1"]) if team1_name else None
    team2_id = upsert_team_and_players(team2_name, players_by_side["team2"], player_main_heroes["team2"]) if team2_name else None

    if team1_id:
        scrim["team1_id"] = team1_id
    if team2_id:
        scrim["team2_id"] = team2_id

    our_slot = normalize_match_team_slot(scrim.get("team_slot", "team1"))
    enemy_slot = "team2" if our_slot == "team1" else "team1"
    scrim["team_id"] = scrim.get(f"{our_slot}_id")
    scrim["team_name"] = str(scrim.get(f"{our_slot}_name", "")).strip()
    scrim["enemy_team_id"] = scrim.get(f"{enemy_slot}_id")
    scrim["enemy_team"] = str(scrim.get(f"{enemy_slot}_name", "")).strip()
    scrim["opponent"] = scrim["enemy_team"]


def _map_name_signature(scrim: dict) -> tuple[str, ...]:
    return tuple(
        _compact_text(map_entry.get("map_name", ""))
        for map_entry in scrim.get("maps", [])
        if isinstance(map_entry, dict) and str(map_entry.get("map_name", "")).strip()
    )


def _find_duplicate_scrim_for_import(imported_scrim: dict, candidates: list[dict] | None = None) -> dict | None:
    imported_team_id = imported_scrim.get("team_id")
    imported_enemy_id = imported_scrim.get("enemy_team_id")
    imported_maps = Counter(_map_name_signature(imported_scrim))
    imported_date = str(imported_scrim.get("scrim_date", "")).strip()
    best_match: dict | None = None
    best_score = -1

    for existing_scrim in (candidates if candidates is not None else SCRIMS):
        if not isinstance(existing_scrim, dict):
            continue
        if str(existing_scrim.get("scrim_date", "")).strip() != imported_date:
            continue

        team_matches = False
        if imported_team_id and existing_scrim.get("team_id") == imported_team_id:
            team_matches = True
        elif _team_names_match(existing_scrim.get("team_name"), imported_scrim.get("team_name")):
            team_matches = True
        if not team_matches:
            continue

        enemy_matches = False
        if imported_enemy_id and existing_scrim.get("enemy_team_id") == imported_enemy_id:
            enemy_matches = True
        elif _team_names_match(existing_scrim.get("enemy_team"), imported_scrim.get("enemy_team")):
            enemy_matches = True
        if not enemy_matches:
            continue

        score = 0
        if imported_team_id and existing_scrim.get("team_id") == imported_team_id:
            score += 20
        if imported_enemy_id and existing_scrim.get("enemy_team_id") == imported_enemy_id:
            score += 20

        existing_maps = Counter(_map_name_signature(existing_scrim))
        overlap = sum((existing_maps & imported_maps).values())
        score += overlap * 5
        if tuple(existing_maps.elements()) == tuple(imported_maps.elements()) and imported_maps:
            score += 15

        if score > best_score:
            best_score = score
            best_match = existing_scrim

    # One-map scrims can still be true duplicates if date/team/opponent/map align,
    # and their score naturally tops out lower than multi-map series.
    return best_match if best_score >= 20 else None


def _merge_imported_map(existing_map: dict, imported_map: dict) -> dict:
    merged_map = copy.deepcopy(imported_map)
    merged_map["id"] = existing_map.get("id")
    if not merged_map.get("notes"):
        merged_map["notes"] = existing_map.get("notes", "")
    if not merged_map.get("vod_url"):
        merged_map["vod_url"] = existing_map.get("vod_url", "")
    if not merged_map.get("events") and merged_map.get("parser_source") not in {"scrimcore-log-structured-import", "scrimcore-log-killfeed-import"}:
        merged_map["events"] = copy.deepcopy(existing_map.get("events", []))
    return merged_map


def _merge_imported_scrim(existing_scrim: dict, imported_scrim: dict) -> None:
    existing_maps = list(existing_scrim.get("maps", []))
    imported_maps = [map_entry for map_entry in imported_scrim.get("maps", []) if isinstance(map_entry, dict)]
    append_unmatched_maps = any(
        map_entry.get("parser_source") in {"scrimcore-log-structured-import", "scrimcore-log-killfeed-import"}
        for map_entry in imported_maps
    )
    indexed_existing_maps: dict[str, list[int]] = defaultdict(list)
    for idx, map_entry in enumerate(existing_maps):
        if not isinstance(map_entry, dict):
            continue
        indexed_existing_maps[_compact_text(map_entry.get("map_name", ""))].append(idx)

    merged_scrim = copy.deepcopy(imported_scrim)
    merged_scrim["id"] = existing_scrim.get("id")
    if not merged_scrim.get("notes"):
        merged_scrim["notes"] = existing_scrim.get("notes", "")

    merged_maps: list[dict] = [copy.deepcopy(map_entry) for map_entry in existing_maps] if append_unmatched_maps else []
    used_indexes: set[int] = set()
    for map_index, imported_map in enumerate(imported_maps):
        map_key = _compact_text(imported_map.get("map_name", ""))
        match_index = next((idx for idx in indexed_existing_maps.get(map_key, []) if idx not in used_indexes), None)
        if not append_unmatched_maps and match_index is None and map_index < len(existing_maps) and map_index not in used_indexes:
            match_index = map_index

        if match_index is None:
            merged_maps.append(copy.deepcopy(imported_map))
            continue

        used_indexes.add(match_index)
        merged_map = _merge_imported_map(existing_maps[match_index], imported_map)
        if append_unmatched_maps and match_index < len(merged_maps):
            merged_maps[match_index] = merged_map
        else:
            merged_maps.append(merged_map)

    for existing_index, existing_map in enumerate(existing_maps):
        if not append_unmatched_maps and existing_index not in used_indexes:
            merged_maps.append(copy.deepcopy(existing_map))

    merged_scrim["maps"] = merged_maps
    existing_scrim.clear()
    existing_scrim.update(merged_scrim)


def _assign_missing_scrim_ids(scrim: dict) -> None:
    global NEXT_MAP_ID, NEXT_EVENT_ID

    for map_entry in scrim.get("maps", []):
        if not isinstance(map_entry, dict):
            continue
        if not isinstance(map_entry.get("id"), int) or int(map_entry.get("id", 0)) <= 0:
            map_entry["id"] = NEXT_MAP_ID
            NEXT_MAP_ID += 1
        for event in map_entry.get("events", []):
            if not isinstance(event, dict):
                continue
            if not isinstance(event.get("id"), int) or int(event.get("id", 0)) <= 0:
                event["id"] = NEXT_EVENT_ID
                NEXT_EVENT_ID += 1


def _recompute_scrim_next_ids_from_state() -> None:
    global NEXT_SCRIM_ID, NEXT_MAP_ID, NEXT_EVENT_ID

    max_scrim_id = 0
    max_map_id = 0
    max_event_id = 0

    for scrim in SCRIMS:
        if not isinstance(scrim, dict):
            continue
        scrim_id = scrim.get("id")
        if isinstance(scrim_id, int):
            max_scrim_id = max(max_scrim_id, scrim_id)

        for map_entry in scrim.get("maps", []):
            if not isinstance(map_entry, dict):
                continue
            map_id = map_entry.get("id")
            if isinstance(map_id, int):
                max_map_id = max(max_map_id, map_id)

            for event in map_entry.get("events", []):
                if not isinstance(event, dict):
                    continue
                event_id = event.get("id")
                if isinstance(event_id, int):
                    max_event_id = max(max_event_id, event_id)

    NEXT_SCRIM_ID = max(1, max_scrim_id + 1)
    NEXT_MAP_ID = max(1, max_map_id + 1)
    NEXT_EVENT_ID = max(1, max_event_id + 1)


def _dedupe_existing_scrims() -> tuple[int, int]:
    if not SCRIMS:
        return 0, 0

    ordered_scrims = sorted(
        [scrim for scrim in SCRIMS if isinstance(scrim, dict)],
        key=lambda scrim: int(scrim.get("id") or 0),
    )
    survivors: list[dict] = []
    duplicates_removed = 0
    merged_updates = 0

    for scrim in ordered_scrims:
        normalize_scrim_record(scrim)
        existing = _find_duplicate_scrim_for_import(scrim, candidates=survivors)
        if existing is None:
            survivors.append(scrim)
            continue

        _merge_imported_scrim(existing, scrim)
        _assign_missing_scrim_ids(existing)
        duplicates_removed += 1
        merged_updates += 1

    if duplicates_removed:
        SCRIMS[:] = survivors
        _recompute_scrim_next_ids_from_state()

    return duplicates_removed, merged_updates


def find_tournament_team_by_id(tournament_teams: list[dict], tournament_team_id: int | None) -> dict | None:
    if tournament_team_id is None:
        return None
    for team in tournament_teams:
        if isinstance(team, dict) and team.get("id") == tournament_team_id:
            return team
    return None


def find_tournament_team_by_name(tournament_teams: list[dict], team_name: str) -> dict | None:
    candidate = str(team_name or "").strip().lower()
    if not candidate:
        return None
    for team in tournament_teams:
        if isinstance(team, dict) and str(team.get("name", "")).strip().lower() == candidate:
            return team
    # Fuzzy fallback using the same match-key logic as the importer
    for team in tournament_teams:
        if isinstance(team, dict) and _team_names_match(team_name, team.get("name", "")):
            return team
    return None


def _resolve_team_from_db(raw_name: str) -> tuple[str, list[str]]:
    """Return (canonical_name, player_list) from the DB for raw_name.
    Falls back to (raw_name, []) if no match is found."""
    resolved = _resolve_team_record_from_db(raw_name)
    if resolved is None:
        return (raw_name or "").strip(), []
    return resolved["name"], resolved["players"]


def _resolve_team_record_from_db(raw_name: str) -> dict | None:
    """Return canonical team metadata from the DB for raw_name when names/aliases match."""
    db = get_db()
    all_teams = db.execute("SELECT id, name FROM teams ORDER BY id").fetchall()
    for row in all_teams:
        if _team_names_match(raw_name, row["name"]):
            players = [
                p["name"]
                for p in db.execute(
                    "SELECT name FROM players WHERE team_id = ? AND COALESCE(is_sub, 0) = 0 ORDER BY name COLLATE NOCASE",
                    (row["id"],),
                ).fetchall()
            ]
            return {"id": int(row["id"]), "name": row["name"], "players": players}
    return None


def sync_tournament_team_with_db(tournament_team: dict) -> bool:
    """Canonicalize a tournament roster from the main teams table when possible."""
    if not isinstance(tournament_team, dict):
        return False

    resolved = None
    source_team_id = tournament_team.get("source_team_id")
    db = get_db()
    if isinstance(source_team_id, int):
        row = db.execute("SELECT id, name FROM teams WHERE id = ?", (source_team_id,)).fetchone()
        if row is not None:
            players = [
                p["name"]
                for p in db.execute(
                    "SELECT name FROM players WHERE team_id = ? AND COALESCE(is_sub, 0) = 0 ORDER BY name COLLATE NOCASE",
                    (row["id"],),
                ).fetchall()
            ]
            resolved = {"id": int(row["id"]), "name": row["name"], "players": players}
    if resolved is None:
        resolved = _resolve_team_record_from_db(tournament_team.get("name", ""))
    if resolved is None:
        return False

    changed = False
    if tournament_team.get("source_team_id") != resolved["id"]:
        tournament_team["source_team_id"] = resolved["id"]
        changed = True
    if (tournament_team.get("name") or "").strip() != resolved["name"]:
        tournament_team["name"] = resolved["name"]
        changed = True

    existing = set(tournament_team.get("players", []))
    for player_name in resolved["players"]:
        if player_name and player_name not in existing:
            tournament_team.setdefault("players", []).append(player_name)
            existing.add(player_name)
            changed = True
    return changed


def get_tournament_team_by_id(tournament_match: dict, tournament_team_id: int | None) -> dict | None:
    return find_tournament_team_by_id(tournament_match.get("tournament_teams", []), tournament_team_id)


def next_tournament_team_id(tournament_match: dict) -> int:
    max_id = 0
    for team in tournament_match.get("tournament_teams", []):
        if isinstance(team, dict) and isinstance(team.get("id"), int):
            max_id = max(max_id, team["id"])
    return max_id + 1


def next_tournament_match_id(tournament_record: dict) -> int:
    max_id = 0
    for tournament_match in tournament_record.get("matches", []):
        if isinstance(tournament_match, dict) and isinstance(tournament_match.get("id"), int):
            max_id = max(max_id, tournament_match["id"])
    return max_id + 1


def _blank_import_draft() -> dict:
    return {
        "team1": {"ban1": "", "protect1": "", "ban2": "", "ban3": "", "ban4": "", "protect2": ""},
        "team2": {"ban1": "", "protect1": "", "ban2": "", "ban3": "", "ban4": "", "protect2": ""},
    }


def _blank_import_comp_section() -> dict:
    return {
        "submap": "",
        "side": "",
        "score": "",
        "team1": [{"hero": "", "player": ""} for _ in range(6)],
        "team2": [{"hero": "", "player": ""} for _ in range(6)],
    }


def _normalize_import_hero(raw_hero: str | None) -> str:
    hero_text = (raw_hero or "").strip()
    if not hero_text or hero_text.lower() == "skipped":
        return ""

    # Imported comp cells can include swaps. The match page stores one hero per
    # player slot, so keep the opening pick as the displayed comp hero.
    hero_text = hero_text.split("/")[0].strip()
    compact = _compact_text(hero_text)
    if compact in {"deadpoolvan", "deadpoolv", "deadpoolvanguard"}:
        return "Tankpool"
    if compact in {"deadpooldps", "deadpoolduelist", "deadpoolduel"}:
        return "DpsPool"
    if compact in {"deadpoolsup", "deadpoolsupp", "deadpoolsupport", "deadpoolstrategist", "deadpoolstrat"} or compact == "suppool":
        return "SupportPool"
    return normalize_hero_slot_value(hero_text)


def _tournament_import_score_match(line: str) -> re.Match | None:
    return re.match(r"^(.+?)\s+(\d+)\s*-\s*(\d+)\s+(.+)$", (line or "").strip())


def _map_import_team_label(label: str, alias_map: dict[str, str]) -> str:
    cleaned = (label or "").strip()
    return alias_map.get(cleaned) or alias_map.get(cleaned.lower()) or alias_map.get(_compact_text(cleaned)) or cleaned


def _set_import_alias(alias_map: dict[str, str], label: str, value: str) -> None:
    cleaned = (label or "").strip()
    if not cleaned:
        return
    alias_map[cleaned] = value
    alias_map[cleaned.lower()] = value
    compact = _compact_text(cleaned)
    if compact:
        alias_map[compact] = value


def _lookup_import_label(mapping: dict[str, str], label: str) -> str:
    cleaned = (label or "").strip()
    return mapping.get(cleaned) or mapping.get(cleaned.lower()) or mapping.get(_compact_text(cleaned)) or ""


def _resolve_import_map_team_labels(left_label: str, right_label: str, team1_name: str, team2_name: str) -> tuple[str, str]:
    left_clean = (left_label or "").strip()
    right_clean = (right_label or "").strip()
    if _team_names_match(left_clean, team1_name) and _team_names_match(right_clean, team2_name):
        return team1_name, team2_name
    if _team_names_match(left_clean, team2_name) and _team_names_match(right_clean, team1_name):
        return team2_name, team1_name
    # Partial match: if only one label is recognisable (e.g. short abbreviation),
    # infer the other team rather than defaulting to left=team1.
    if _team_names_match(left_clean, team1_name):
        return team1_name, team2_name
    if _team_names_match(left_clean, team2_name):
        return team2_name, team1_name
    if _team_names_match(right_clean, team2_name):
        return team1_name, team2_name
    if _team_names_match(right_clean, team1_name):
        return team2_name, team1_name
    return team1_name, team2_name


def _assign_import_draft_row(draft: dict, side: str, headers: list[str], values: list[str]) -> None:
    if side not in TEAM_SLOTS:
        return

    ban_index = 1
    protect_index = 1
    for header, raw_value in zip(headers, values):
        hero_name = _normalize_import_hero(raw_value)
        if not hero_name:
            continue

        header_value = (header or "").strip().lower()
        if header_value.startswith("ban") and ban_index <= 4:
            draft[side][f"ban{ban_index}"] = hero_name
            ban_index += 1
        elif header_value.startswith("protect") and protect_index <= 2:
            draft[side][f"protect{protect_index}"] = hero_name
            protect_index += 1


def _split_import_table_row(line: str) -> list[str]:
    if "\t" in (line or ""):
        return [cell.strip() for cell in line.split("\t")]
    return [cell.strip() for cell in re.split(r"\s{2,}", (line or "").strip()) if cell.strip()]


def _parse_import_draft(block_lines: list[str], side_by_label: dict[str, str]) -> dict:
    draft = _blank_import_draft()
    for index, line in enumerate(block_lines):
        cells = _split_import_table_row(line)
        if not cells or cells[0].lower() != "ban":
            continue

        headers = cells
        for row_offset, row in enumerate(block_lines[index + 1:index + 3]):
            row_cells = _split_import_table_row(row)
            if len(row_cells) < 2:
                continue
            side = _lookup_import_label(side_by_label, row_cells[0])
            if side not in TEAM_SLOTS:
                side = "team1" if row_offset == 0 else "team2"
            _assign_import_draft_row(draft, side, headers, row_cells[1:])
        break
    return draft


def _parse_import_comp(block_lines: list[str], side_by_label: dict[str, str]) -> dict:
    section = _blank_import_comp_section()
    for index, line in enumerate(block_lines):
        cells = _split_import_table_row(line)
        hero_columns = [cell_index for cell_index, cell in enumerate(cells) if cell.lower() == "hero"]
        if len(cells) < 4 or len(hero_columns) < 2 or hero_columns[0] != 1:
            continue

        right_hero_index = hero_columns[-1]
        left_side = _lookup_import_label(side_by_label, cells[0])
        right_side = _lookup_import_label(side_by_label, cells[-1])
        if left_side not in TEAM_SLOTS:
            left_side = "team1"
        if right_side not in TEAM_SLOTS:
            right_side = "team2"

        for row in block_lines[index + 1:index + 7]:
            row_cells = _split_import_table_row(row)
            if len(row_cells) <= right_hero_index or len(row_cells) < 4:
                continue

            left_slot = {"player": row_cells[0], "hero": _normalize_import_hero(row_cells[1])}
            right_slot = {"player": row_cells[-1], "hero": _normalize_import_hero(row_cells[right_hero_index])}

            if len(section[left_side]) < 6:
                section[left_side].append(left_slot)
            else:
                empty_index = next((i for i, slot in enumerate(section[left_side]) if not slot.get("hero") and not slot.get("player")), None)
                if empty_index is not None:
                    section[left_side][empty_index] = left_slot

            if len(section[right_side]) < 6:
                section[right_side].append(right_slot)
            else:
                empty_index = next((i for i, slot in enumerate(section[right_side]) if not slot.get("hero") and not slot.get("player")), None)
                if empty_index is not None:
                    section[right_side][empty_index] = right_slot
        break

    return section


def parse_tournament_match_text_import(raw_text: str) -> dict:
    lines = [(line or "").rstrip("\r") for line in (raw_text or "").splitlines()]
    non_empty_lines = [line.strip() for line in lines if line.strip()]
    match_line = ""
    match = None
    for line in non_empty_lines:
        candidate = _tournament_import_score_match(line)
        if candidate:
            match_line = line
            match = candidate
            break

    if match is None:
        raise ValueError("Could not find a match score line like Team A 3-2 Team B.")

    team1_name = match.group(1).strip()
    team2_name = match.group(4).strip()
    match_score = f"{match.group(2)} - {match.group(3)}"
    notes = non_empty_lines[0] if non_empty_lines and non_empty_lines[0] != match_line else ""

    map_start_indices = [index for index, line in enumerate(lines) if re.match(r"^\s*MAP\s+\d+\s+-", line, re.IGNORECASE)]
    if not map_start_indices:
        raise ValueError("Could not find any MAP sections in the uploaded file.")

    first_score_match = None
    for line in lines[map_start_indices[0] + 1:]:
        first_score_match = _tournament_import_score_match(line)
        if first_score_match:
            break
    alias_map: dict[str, str] = {}
    if first_score_match:
        first_left = first_score_match.group(1).strip()
        first_right = first_score_match.group(4).strip()
        first_left_team, first_right_team = _resolve_import_map_team_labels(first_left, first_right, team1_name, team2_name)
        _set_import_alias(alias_map, first_left, first_left_team)
        _set_import_alias(alias_map, first_right, first_right_team)

    maps: list[dict] = []
    players_by_team: dict[str, set[str]] = {team1_name: set(), team2_name: set()}
    for position, start_index in enumerate(map_start_indices):
        end_index = map_start_indices[position + 1] if position + 1 < len(map_start_indices) else len(lines)
        block_lines = lines[start_index:end_index]
        header = block_lines[0].strip()
        header_match = re.match(r"^MAP\s+\d+\s+-\s*([^:]+):\s*(.+)$", header, re.IGNORECASE)
        map_name = _match_map_name(header_match.group(2).strip() if header_match else header)

        score_match = next((_tournament_import_score_match(line) for line in block_lines[1:] if _tournament_import_score_match(line)), None)
        if score_match:
            left_label = score_match.group(1).strip()
            right_label = score_match.group(4).strip()
            left_score = score_match.group(2)
            right_score = score_match.group(3)
        else:
            left_label = team1_name
            right_label = team2_name
            left_score = ""
            right_score = ""

        replay_line = next(
            (line.strip() for line in block_lines if re.match(r"^\s*Replay\s+ID\s*:", line, re.IGNORECASE)),
            "",
        )
        left_team_name = _map_import_team_label(left_label, alias_map)
        right_team_name = _map_import_team_label(right_label, alias_map)
        side_by_label: dict[str, str] = {}
        for label in (left_label, left_team_name):
            _set_import_alias(side_by_label, label, "team1")
        for label in (right_label, right_team_name):
            _set_import_alias(side_by_label, label, "team2")

        draft = _parse_import_draft(block_lines, side_by_label)
        comp_section = _parse_import_comp(block_lines, side_by_label)
        for side, team_name in (("team1", left_team_name), ("team2", right_team_name)):
            players_by_team.setdefault(team_name, set())
            for slot in comp_section.get(side, []):
                player_name = (slot.get("player") or "").strip()
                if player_name:
                    players_by_team[team_name].add(player_name)

        score = f"{left_score} - {right_score}" if left_score and right_score else ""
        our_team_slot = "team1" if _team_names_match(left_team_name, team1_name) else "team2"
        maps.append({
            "map_name": map_name,
            "map_type": DEFAULT_MAP_TYPE,
            "side": "",
            "our_team_slot": our_team_slot,
            "result": infer_result_from_score_text(score, slot=our_team_slot) if score else "",
            "score": score,
            "team1_name": left_team_name,
            "team2_name": right_team_name,
            "draft": draft,
            "comp": [comp_section],
            "notes": replay_line,
            "vod_url": "",
            "events": [],
        })

    return {
        "team1_name": team1_name,
        "team2_name": team2_name,
        "match_score": match_score,
        "notes": notes,
        "maps": maps,
        "players_by_team": {team_name: sorted(players) for team_name, players in players_by_team.items()},
    }


def find_or_add_tournament_team_from_import(tournament_record: dict, team_name: str, players: list[str] | None = None) -> dict:
    # Resolve canonical name + roster from the DB first
    resolved_team = _resolve_team_record_from_db(team_name)
    source_team_id = resolved_team["id"] if resolved_team else None
    db_canonical = resolved_team["name"] if resolved_team else (team_name or "").strip()
    db_players = resolved_team["players"] if resolved_team else []
    # Merge imported players with any DB roster entries
    merged_players = list(db_players)
    for p in (players or []):
        if p and p not in merged_players:
            merged_players.append(p)

    # Match against an existing tournament team using the canonical name
    existing_team = find_tournament_team_by_name(tournament_record.get("tournament_teams", []), db_canonical)
    if existing_team is not None:
        if source_team_id is not None and existing_team.get("source_team_id") != source_team_id:
            existing_team["source_team_id"] = source_team_id
        if db_canonical and (existing_team.get("name") or "").strip() != db_canonical:
            existing_team["name"] = db_canonical
        existing_players = existing_team.setdefault("players", [])
        for player_name in merged_players:
            if player_name and player_name not in existing_players:
                existing_players.append(player_name)
        return existing_team

    new_team = {
        "id": next_tournament_team_id(tournament_record),
        "name": db_canonical or "Unknown Team",
        "players": [p for p in merged_players if p],
    }
    if source_team_id is not None:
        new_team["source_team_id"] = source_team_id
    tournament_record.setdefault("tournament_teams", []).append(new_team)
    return new_team


