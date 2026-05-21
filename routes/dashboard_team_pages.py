# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

@app.route("/")
def dashboard():
    db = get_db()
    page_size = 6
    try:
        current_page = max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        current_page = 1

    scrims_desc = list(reversed(SCRIMS))
    total_scrim_records = len(scrims_desc)
    total_scrim_pages = max(1, (total_scrim_records + page_size - 1) // page_size)
    current_page = min(current_page, total_scrim_pages)
    start_index = (current_page - 1) * page_size
    end_index = start_index + page_size
    paged_scrims = scrims_desc[start_index:end_index]

    total_scrims = len(SCRIMS)
    total_tournaments = len(TOURNAMENT_MATCHES)
    total_maps = sum(len(scrim["maps"]) for scrim in SCRIMS) + sum(len(match["maps"]) for match in TOURNAMENT_MATCHES)
    total_events = (
        sum(len(map_entry["events"]) for scrim in SCRIMS for map_entry in scrim["maps"])
        + sum(len(map_entry.get("events", [])) for match in TOURNAMENT_MATCHES for map_entry in match["maps"])
    )
    total_teams = db.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    total_players = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]

    personal_team_rows = db.execute(
        "SELECT id, name FROM teams WHERE is_personal = 1"
    ).fetchall()

    pick_counter: Counter = Counter()
    ban_counter: Counter = Counter()
    protect_counter: Counter = Counter()
    comp_counter: Counter = Counter()
    opponent_records = defaultdict(lambda: {"team_id": None, "name": "", "scrims": 0, "maps": 0})
    personal_quick_teams = []
    seen_scrims: set = set()

    for team_row in personal_team_rows:
        team_scrims = get_scrims_for_team(team_row["id"], team_row["name"])
        personal_quick_teams.append(
            {
                "id": team_row["id"],
                "name": team_row["name"],
                "scrims": len(team_scrims),
                "maps": sum(len(scrim.get("maps", [])) for scrim in team_scrims),
            }
        )
        for scrim in team_scrims:
            scrim_id = scrim.get("id")
            if scrim_id in seen_scrims:
                continue
            seen_scrims.add(scrim_id)

            opponent_name = (
                (scrim.get("enemy_team", "") or "").strip()
                or (scrim.get("opponent", "") or "").strip()
                or "Opponent"
            )
            opponent_team_id = scrim.get("enemy_team_id")
            opponent_key = (
                f"id:{int(opponent_team_id)}"
                if isinstance(opponent_team_id, int) and opponent_team_id > 0
                else f"name:{opponent_name.lower()}"
            )
            opponent_records[opponent_key]["team_id"] = opponent_team_id if isinstance(opponent_team_id, int) and opponent_team_id > 0 else None
            opponent_records[opponent_key]["name"] = opponent_name
            opponent_records[opponent_key]["scrims"] += 1
            opponent_records[opponent_key]["maps"] += len(scrim.get("maps", []))

            for map_entry in scrim.get("maps", []):
                if not isinstance(map_entry, dict):
                    continue
                our_slot = map_entry.get("our_team_slot", "team1")
                for section in map_entry.get("comp", []):
                    if not isinstance(section, dict):
                        continue
                    comp_heroes: list[str] = []
                    for slot in section.get(our_slot, []):
                        if not isinstance(slot, dict):
                            continue
                        hero = canonicalize_hero_name(slot.get("hero", ""))
                        if hero:
                            pick_counter[hero] += 1
                            comp_heroes.append(hero)
                    if len(comp_heroes) >= 4:
                        comp_counter[tuple(comp_heroes)] += 1
                draft = map_entry.get("draft", {})
                if isinstance(draft, dict):
                    our_draft = draft.get(our_slot, {})
                    if isinstance(our_draft, dict):
                        for ban_key in ("ban1", "ban2", "ban3", "ban4"):
                            hero = canonicalize_hero_name(our_draft.get(ban_key, ""))
                            if hero:
                                ban_counter[hero] += 1
                        for protect_key in ("protect1", "protect2"):
                            hero = canonicalize_hero_name(our_draft.get(protect_key, ""))
                            if hero:
                                protect_counter[hero] += 1

    top_picks = [{"hero": h, "count": c} for h, c in pick_counter.most_common(5)]
    top_bans = [{"hero": h, "count": c} for h, c in ban_counter.most_common(5)]
    top_protects = [{"hero": h, "count": c} for h, c in protect_counter.most_common(5)]
    top_comp_shells = [
        {"heroes": list(comp), "count": count}
        for comp, count in comp_counter.most_common(4)
    ]
    personal_quick_teams.sort(
        key=lambda row: (row["maps"], row["scrims"], row["name"].lower()),
        reverse=True,
    )
    quick_opponents = sorted(
        opponent_records.values(),
        key=lambda row: (row["maps"], row["scrims"], row["name"]),
        reverse=True,
    )[:8]

    all_team_rows = db.execute(
        """
        SELECT id, name, logo_path, is_personal, quality_tag
        FROM teams
        ORDER BY
            CASE quality_tag
                WHEN 'Preferred' THEN 0
                WHEN 'Semi Preferred' THEN 1
                WHEN 'Good' THEN 2
                WHEN 'Avoid' THEN 3
                ELSE 4
            END,
            name COLLATE NOCASE
        LIMIT 8
        """
    ).fetchall()
    all_teams_for_quick_access = [
        {
            "id": row["id"],
            "name": row["name"],
            "logo_path": row["logo_path"],
            "is_personal": bool(row["is_personal"]),
            "quality_tag": (row["quality_tag"] or "").strip(),
        }
        for row in all_team_rows
    ]
    dashboard_sim_teams = [
        {
            "id": row["id"],
            "name": row["name"],
            "is_personal": bool(row["is_personal"]),
        }
        for row in all_team_rows
    ]
    dashboard_default_team_id = None
    for row in dashboard_sim_teams:
        if row["is_personal"]:
            dashboard_default_team_id = row["id"]
            break
    if dashboard_default_team_id is None and dashboard_sim_teams:
        dashboard_default_team_id = dashboard_sim_teams[0]["id"]
    dashboard_compare_team_id = None
    for row in dashboard_sim_teams:
        if row["id"] != dashboard_default_team_id:
            dashboard_compare_team_id = row["id"]
            break
    if dashboard_compare_team_id is None:
        dashboard_compare_team_id = dashboard_default_team_id

    dashboard_season_options = get_scrim_season_options(SCRIMS)
    dashboard_has_unseasoned_scrims = any(
        not normalize_season_value(scrim.get("season", ""))
        for scrim in SCRIMS
    )
    dashboard_default_season = get_current_season_from_recent_scrim(SCRIMS)
    teams = db.execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    today = date.today().strftime("%Y-%m-%d")

    return render_template(
        "dashboard.html",
        total_scrims=total_scrims,
        total_tournaments=total_tournaments,
        total_maps=total_maps,
        total_events=total_events,
        total_teams=total_teams,
        total_players=total_players,
        recent_scrims=paged_scrims,
        scrim_page=current_page,
        scrim_page_size=page_size,
        scrim_total_pages=total_scrim_pages,
        scrim_total_count=total_scrim_records,
        recent_tournaments=list(reversed(TOURNAMENT_MATCHES[-5:])),
        top_picks=top_picks,
        top_bans=top_bans,
        top_protects=top_protects,
        top_comp_shells=top_comp_shells,
        personal_quick_teams=personal_quick_teams,
        quick_opponents=quick_opponents,
        all_teams_for_quick_access=all_teams_for_quick_access,
        dashboard_sim_teams=dashboard_sim_teams,
        dashboard_default_team_id=dashboard_default_team_id,
        dashboard_compare_team_id=dashboard_compare_team_id,
        dashboard_season_options=dashboard_season_options,
        dashboard_default_season=dashboard_default_season,
        dashboard_has_unseasoned_scrims=dashboard_has_unseasoned_scrims,
        teams=teams,
        today=today,
        map_type_options=MAP_TYPES,
        maps=MAPS,
        hero_roles=HERO_ROLES,
    )


@app.route("/team-scouting")
def team_scouting():
    db = get_db()
    migrate_enemy_teams_to_team_database(db)

    our_team_row = db.execute(
        """
        SELECT id, name
        FROM teams
        ORDER BY
            CASE WHEN is_personal = 1 THEN 0 ELSE 1 END,
            CASE quality_tag
                WHEN 'Preferred' THEN 0
                WHEN 'Semi Preferred' THEN 1
                WHEN 'Good' THEN 2
                WHEN 'Avoid' THEN 3
                ELSE 4
            END,
            name COLLATE NOCASE
        LIMIT 1
        """
    ).fetchone()

    if our_team_row is None:
        return render_template(
            "team_scouting.html",
            our_team=None,
            scout_rows=[],
        )

    our_team = {"id": int(our_team_row["id"]), "name": (our_team_row["name"] or "").strip()}
    team_rows = db.execute(
        """
        SELECT id, name, logo_path, quality_tag, is_personal
        FROM teams
        ORDER BY
            CASE quality_tag
                WHEN 'Preferred' THEN 0
                WHEN 'Semi Preferred' THEN 1
                WHEN 'Good' THEN 2
                WHEN 'Avoid' THEN 3
                ELSE 4
            END,
            name COLLATE NOCASE
        """
    ).fetchall()
    opponent_rows = db.execute(
        """
        SELECT id, name, logo_path, quality_tag, is_personal
        FROM teams
        WHERE id != ?
        ORDER BY
            CASE quality_tag
                WHEN 'Preferred' THEN 0
                WHEN 'Semi Preferred' THEN 1
                WHEN 'Good' THEN 2
                WHEN 'Avoid' THEN 3
                ELSE 4
            END,
            name COLLATE NOCASE
        """,
        (our_team["id"],),
    ).fetchall()

    scrim_history = get_scrims_for_team(our_team["id"], our_team["name"])
    tournament_history = build_team_tournament_scrims({"id": our_team["id"], "name": our_team["name"]})
    our_history = scrim_history + tournament_history
    season_options = get_scrim_season_options(our_history)
    default_season = get_current_season_from_recent_scrim(our_history)
    has_unseasoned_scrims = any(
        not normalize_season_value(scrim.get("season", ""))
        for scrim in our_history
    )
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )

    map_options = list(MAPS)
    selected_map_name = (request.args.get("map_name") or "all").strip()
    if selected_map_name != "all" and selected_map_name not in map_options:
        selected_map_name = "all"

    try:
        min_maps = int(request.args.get("min_maps") or 3)
    except (TypeError, ValueError):
        min_maps = 3
    min_maps = max(1, min(50, min_maps))

    filtered_scrim_history = filter_scrims_by_season(scrim_history, selected_season)
    filtered_tournament_history = filter_scrims_by_season(tournament_history, selected_season)

    def _filter_scrims_to_map(scrims: list[dict], map_name: str) -> list[dict]:
        if not map_name or map_name == "all":
            return scrims
        target = map_name.strip().lower()
        filtered: list[dict] = []
        for scrim in scrims:
            maps_for_target = [
                map_entry
                for map_entry in scrim.get("maps", [])
                if ((map_entry.get("map_name") or map_entry.get("map") or "").strip().lower() == target)
            ]
            if not maps_for_target:
                continue
            copied = dict(scrim)
            copied["maps"] = maps_for_target
            filtered.append(copied)
        return filtered

    filtered_scrim_history = _filter_scrims_to_map(filtered_scrim_history, selected_map_name)
    filtered_tournament_history = _filter_scrims_to_map(filtered_tournament_history, selected_map_name)
    filtered_history = filtered_scrim_history + filtered_tournament_history

    def _opposite_slot(slot: str) -> str:
        return "team2" if slot == "team1" else "team1"

    def _name_keys(value: str) -> set[str]:
        raw = str(value or "").strip().lower()
        if not raw:
            return set()
        compact = "".join(ch for ch in raw if ch.isalnum())
        tokens = " ".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in raw)
        token_compact = " ".join(tokens.split())
        keys = {raw}
        if compact:
            keys.add(compact)
        if token_compact:
            keys.add(token_compact)
            keys.add("".join(token_compact.split()))
        return {k for k in keys if k}

    def _names_match(left: str, right: str) -> bool:
        left_keys = _name_keys(left)
        right_keys = _name_keys(right)
        if not left_keys or not right_keys:
            return False
        return bool(left_keys & right_keys)

    def _scrim_includes_opponent(scrim: dict, opponent_id: int, opponent_name: str) -> bool:
        if int(scrim.get("enemy_team_id") or 0) == opponent_id:
            return True
        if int(scrim.get("team1_id") or 0) == opponent_id or int(scrim.get("team2_id") or 0) == opponent_id:
            return True
        target = (opponent_name or "").strip()
        if not target:
            return False
        names = [
            (scrim.get("enemy_team") or "").strip(),
            (scrim.get("opponent") or "").strip(),
            (scrim.get("team1_name") or "").strip(),
            (scrim.get("team2_name") or "").strip(),
        ]
        return any(_names_match(name, target) for name in names if name)

    def _extract_bans_from_slot(map_entry: dict, slot: str) -> list[str]:
        draft = map_entry.get("draft", {})
        side_draft = draft.get(slot, {}) if isinstance(draft, dict) else {}
        if not isinstance(side_draft, dict):
            return []
        bans: list[str] = []
        for ban_key in ("ban1", "ban2", "ban3", "ban4"):
            hero_name = canonicalize_hero_name(side_draft.get(ban_key, ""))
            if hero_name:
                bans.append(hero_name)
        return bans

    def _build_our_bans_counter(opponent_id: int, opponent_name: str) -> Counter:
        counter: Counter = Counter()
        for scrim in filtered_history:
            if not _scrim_includes_opponent(scrim, opponent_id, opponent_name):
                continue
            for map_entry in scrim.get("maps", []):
                if not isinstance(map_entry, dict):
                    continue
                our_slot = (map_entry.get("our_team_slot") or "team1").strip()
                if our_slot not in TEAM_SLOTS:
                    our_slot = "team1"
                for hero_name in _extract_bans_from_slot(map_entry, our_slot):
                    counter[hero_name] += 1
        return counter

    def _build_tournament_field_bans_counter(opponent_id: int, opponent_name: str) -> Counter:
        counter: Counter = Counter()
        target_name = (opponent_name or "").strip()
        selected_season_key = normalize_season_value(selected_season)
        selected_map_key = (selected_map_name or "all").strip().lower()

        for tournament_record in TOURNAMENT_MATCHES:
            tournament_season = normalize_season_value(tournament_record.get("season", ""))
            if selected_season_key and selected_season_key != "all" and tournament_season != selected_season_key:
                continue

            opponent_tournament_ids: set[int] = set()
            for tournament_team in tournament_record.get("tournament_teams", []):
                if not isinstance(tournament_team, dict):
                    continue
                source_team_id = tournament_team.get("source_team_id")
                tournament_team_name = (tournament_team.get("name") or "").strip()
                name_match = False
                if target_name and tournament_team_name:
                    name_match = _names_match(tournament_team_name, target_name)
                if (isinstance(source_team_id, int) and source_team_id == opponent_id) or name_match:
                    tournament_team_id = tournament_team.get("id")
                    if isinstance(tournament_team_id, int):
                        opponent_tournament_ids.add(tournament_team_id)

            if not opponent_tournament_ids:
                continue

            for tournament_match in tournament_record.get("matches", []):
                if not isinstance(tournament_match, dict):
                    continue
                match_team1_id = tournament_match.get("team1_tournament_team_id")
                match_team2_id = tournament_match.get("team2_tournament_team_id")

                default_target_slot = None
                if match_team1_id in opponent_tournament_ids:
                    default_target_slot = "team1"
                elif match_team2_id in opponent_tournament_ids:
                    default_target_slot = "team2"
                if default_target_slot is None:
                    continue

                for map_entry in tournament_match.get("maps", []):
                    if not isinstance(map_entry, dict):
                        continue
                    if selected_map_key != "all":
                        map_name_key = ((map_entry.get("map_name") or map_entry.get("map") or "").strip().lower())
                        if map_name_key != selected_map_key:
                            continue

                    map_target_slot = default_target_slot
                    map_team1_id = map_entry.get("team1_tournament_team_id")
                    map_team2_id = map_entry.get("team2_tournament_team_id")
                    if map_team1_id in opponent_tournament_ids:
                        map_target_slot = "team1"
                    elif map_team2_id in opponent_tournament_ids:
                        map_target_slot = "team2"

                    enemy_slot = _opposite_slot(map_target_slot)
                    for hero_name in _extract_bans_from_slot(map_entry, enemy_slot):
                        counter[hero_name] += 1
        return counter

    def _build_ban_correlation_rows(opponent_id: int, opponent_name: str) -> list[dict]:
        our_bans = _build_our_bans_counter(opponent_id, opponent_name)
        field_bans = _build_tournament_field_bans_counter(opponent_id, opponent_name)
        rows: list[dict] = []

        for hero_name in set(our_bans.keys()) | set(field_bans.keys()):
            our_count = int(our_bans.get(hero_name, 0))
            field_count = int(field_bans.get(hero_name, 0))
            combined = our_count + field_count
            overlap = min(our_count, field_count)
            if combined <= 0:
                continue

            diff = our_count - field_count
            if overlap > 0 and abs(diff) <= 1:
                priority_label = "Shared Priority"
                priority_key = "shared"
            elif field_count > our_count:
                priority_label = "Field Priority"
                priority_key = "field"
            elif our_count > field_count:
                priority_label = "Our Priority"
                priority_key = "ours"
            else:
                priority_label = "Even Priority"
                priority_key = "even"

            rows.append(
                {
                    "hero": hero_name,
                    "our_bans": our_count,
                    "field_bans": field_count,
                    "combined": combined,
                    "overlap": overlap,
                    "priority_label": priority_label,
                    "priority_key": priority_key,
                    "alignment_pct": round((overlap / combined) * 100, 1) if combined else 0.0,
                }
            )

        rows.sort(
            key=lambda item: (
                item["overlap"],
                item["combined"],
                item["field_bans"],
                item["our_bans"],
                item["hero"],
            ),
            reverse=True,
        )
        return rows[:8]

    def _build_scout_summary(
        history_rows: list[dict],
        opponent_id: int,
        opponent_name: str,
        *,
        include_all_matches: bool = False,
    ) -> dict | None:
        enemy_hero_counter: Counter = Counter()
        enemy_hero_record = defaultdict(lambda: {"wins": 0, "losses": 0, "maps": 0})
        ban_counter: Counter = Counter()
        ban_record = defaultdict(lambda: {"wins": 0, "losses": 0, "maps": 0})
        comp_counter: Counter = Counter()
        maps = 0
        wins = 0
        losses = 0

        for scrim in history_rows:
            if not include_all_matches and not _scrim_includes_opponent(scrim, opponent_id, opponent_name):
                continue
            for map_entry in scrim.get("maps", []):
                if not isinstance(map_entry, dict):
                    continue
                our_slot = (map_entry.get("our_team_slot") or "team1").strip()
                if our_slot not in TEAM_SLOTS:
                    our_slot = "team1"
                opponent_slot = _opposite_slot(our_slot)
                result = get_map_outcome_for_slot(map_entry, our_slot)

                maps += 1
                if result == "Win":
                    wins += 1
                elif result == "Loss":
                    losses += 1

                draft = map_entry.get("draft", {})
                opponent_draft = draft.get(opponent_slot, {}) if isinstance(draft, dict) else {}
                if isinstance(opponent_draft, dict):
                    for ban_key in ("ban1", "ban2", "ban3", "ban4"):
                        hero = canonicalize_hero_name(opponent_draft.get(ban_key, ""))
                        if not hero:
                            continue
                        ban_counter[hero] += 1
                        ban_record[hero]["maps"] += 1
                        if result == "Win":
                            ban_record[hero]["wins"] += 1
                        elif result == "Loss":
                            ban_record[hero]["losses"] += 1

                for section in map_entry.get("comp", []):
                    if not isinstance(section, dict):
                        continue
                    heroes = [
                        canonicalize_hero_name(slot.get("hero", ""))
                        for slot in section.get(opponent_slot, [])
                        if isinstance(slot, dict) and canonicalize_hero_name(slot.get("hero", ""))
                    ]
                    for hero_name in heroes:
                        enemy_hero_counter[hero_name] += 1
                        enemy_hero_record[hero_name]["maps"] += 1
                        if result == "Win":
                            enemy_hero_record[hero_name]["wins"] += 1
                        elif result == "Loss":
                            enemy_hero_record[hero_name]["losses"] += 1
                    if len(heroes) >= 4:
                        comp_counter[tuple(heroes)] += 1

        if maps < min_maps:
            return None

        recommended_bans = []
        for hero_name, count in enemy_hero_counter.most_common(10):
            record = enemy_hero_record[hero_name]
            decided = int(record["wins"]) + int(record["losses"])
            opponent_win_rate = round((int(record["losses"]) / decided) * 100, 1) if decided else 0.0
            recommended_bans.append(
                {
                    "hero": hero_name,
                    "count": count,
                    "opponent_win_rate": opponent_win_rate,
                    "wins": int(record["wins"]),
                    "losses": int(record["losses"]),
                }
            )

        recommended_bans.sort(
            key=lambda item: (item["count"], item["opponent_win_rate"], item["losses"]),
            reverse=True,
        )
        recommended_bans = recommended_bans[:5]

        top_enemy_bans = []
        for hero_name, count in ban_counter.most_common(8):
            record = ban_record[hero_name]
            decided = int(record["wins"]) + int(record["losses"])
            our_loss_rate = round((int(record["losses"]) / decided) * 100, 1) if decided else 0.0
            top_enemy_bans.append(
                {
                    "hero": hero_name,
                    "count": count,
                    "our_loss_rate": our_loss_rate,
                    "wins": int(record["wins"]),
                    "losses": int(record["losses"]),
                }
            )

        top_enemy_bans.sort(
            key=lambda item: (item["our_loss_rate"], item["count"], item["losses"]),
            reverse=True,
        )
        top_enemy_bans = top_enemy_bans[:5]

        favorite_comps = [
            {"heroes": list(comp), "count": count}
            for comp, count in comp_counter.most_common(4)
        ]

        return {
            "maps": maps,
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / (wins + losses)) * 100, 1) if (wins + losses) else 0.0,
            "recommended_bans": recommended_bans,
            "top_enemy_bans": top_enemy_bans,
            "favorite_comps": favorite_comps,
        }

    filter_context_bits = []
    if selected_season and selected_season != "all":
        filter_context_bits.append(f"season {selected_season}")
    if selected_map_name and selected_map_name != "all":
        filter_context_bits.append(f"map {selected_map_name}")
    if min_maps:
        filter_context_bits.append(f"minimum {min_maps} maps")
    filter_context = ", ".join(filter_context_bits) if filter_context_bits else "all history"

    def _build_ai_url(opponent_id: int, opponent_name: str, prompt: str) -> str:
        return url_for(
            "machine",
            tab="chat",
            team_a_id=our_team["id"],
            team_b_id=opponent_id,
            team_a_name=our_team["name"],
            team_b_name=opponent_name,
            prompt=prompt,
        )

    def _build_opening_priority_rows(history_rows: list[dict]) -> dict:
        ban_counter: Counter = Counter()
        protect_counter: Counter = Counter()

        for scrim in history_rows:
            for map_entry in scrim.get("maps", []):
                if not isinstance(map_entry, dict):
                    continue
                our_slot = (map_entry.get("our_team_slot") or "team1").strip()
                if our_slot not in TEAM_SLOTS:
                    our_slot = "team1"
                our_draft = ((map_entry.get("draft") or {}).get(our_slot) or {})
                opening_ban = _canonical_draft_hero(our_draft.get("ban1", ""))
                opening_protect = _canonical_draft_hero(our_draft.get("protect1", ""))
                if opening_ban:
                    ban_counter[opening_ban] += 1
                if opening_protect:
                    protect_counter[opening_protect] += 1

        total_bans = sum(ban_counter.values())
        total_protects = sum(protect_counter.values())
        return {
            "ban1_rows": [
                {
                    "hero": hero_name,
                    "count": count,
                    "rate": round((count / total_bans) * 100, 1) if total_bans else 0.0,
                }
                for hero_name, count in ban_counter.most_common(5)
            ],
            "protect1_rows": [
                {
                    "hero": hero_name,
                    "count": count,
                    "rate": round((count / total_protects) * 100, 1) if total_protects else 0.0,
                }
                for hero_name, count in protect_counter.most_common(5)
            ],
        }

    def _build_draft_scout_bundle(team_name: str, history_rows: list[dict], analytics: dict, tree_model: dict) -> dict:
        opening_rows = _build_opening_priority_rows(history_rows)
        comp_rows = analytics.get("comp_rows") or tree_model.get("comp_rows") or []
        ban_rows = analytics.get("ban_rows") or []
        protect_rows = analytics.get("protect_rows") or []
        ban_line_rows = tree_model.get("ban_line_rows") or []
        map_draft_rows = analytics.get("map_draft_rows") or []
        most_likely_ban_route_rows = analytics.get("most_likely_ban_route_rows") or []
        protect1_influence_rows = analytics.get("protect1_influence_rows") or []
        comp_archetype_rows = analytics.get("comp_archetype_rows") or []
        comfort_core_rows = tree_model.get("comfort_core_rows") or []
        hero_pool_rows = tree_model.get("hero_pool_rows") or []
        player_hero_rows = tree_model.get("player_hero_rows") or []
        pivot_rows = tree_model.get("pivot_rows") or []
        mode_comp_rows = tree_model.get("mode_comp_rows") or []
        comp_path_rows = tree_model.get("comp_path_rows") or []
        equivalent_path_rows = tree_model.get("equivalent_path_rows") or []
        volatile_hero_rows = tree_model.get("volatile_hero_rows") or []
        map_state_rows = tree_model.get("map_state_rows") or []

        top_comp = comp_rows[0] if comp_rows else {}
        top_ban = ban_rows[0] if ban_rows else {}
        top_protect = protect_rows[0] if protect_rows else {}
        top_path = comp_path_rows[0] if comp_path_rows else {}
        top_route = most_likely_ban_route_rows[0] if most_likely_ban_route_rows else {}

        top_path_share = float(top_path.get("top_path_share", 0) or 0)
        route_confidence = float(top_route.get("top_rate", 0) or 0)
        predictability_score = round(min(100.0, (top_path_share * 0.65) + (route_confidence * 0.35)), 1)

        avg_path_count = (
            sum(float(row.get("path_count", 0) or 0) for row in comp_path_rows[:3]) / min(len(comp_path_rows), 3)
            if comp_path_rows else 0.0
        )
        avg_path_deviation = (
            sum(float(row.get("avg_deviation", 0) or 0) for row in comp_path_rows[:3]) / min(len(comp_path_rows), 3)
            if comp_path_rows else 0.0
        )
        flexibility_score = round(
            min(
                100.0,
                len(hero_pool_rows[:8]) * 7.0
                + len(pivot_rows[:4]) * 8.0
                + len(equivalent_path_rows[:4]) * 7.0
                + avg_path_count * 4.0,
            ),
            1,
        )
        adaptation_score = round(
            min(
                100.0,
                sum(len(row.get("pivots", [])) for row in pivot_rows[:3]) * 9.0
                + len(mode_comp_rows[:4]) * 6.0
                + avg_path_deviation * 10.0,
            ),
            1,
        )

        top_archetype = max(comp_archetype_rows, key=lambda row: float(row.get("main_rate", 0) or 0)) if comp_archetype_rows else None
        avg_core_comfort = (
            sum(float(row.get("comfort_rate", 0) or 0) for row in comfort_core_rows[:4]) / min(len(comfort_core_rows), 4)
            if comfort_core_rows else 0.0
        )

        identity_tags: list[str] = []
        if top_archetype and float(top_archetype.get("main_rate", 0) or 0) >= 35:
            identity_tags.append(top_archetype.get("label") or "Structured comps")
        if avg_core_comfort >= 45:
            identity_tags.append("Comfort heavy")
        if predictability_score >= 65:
            identity_tags.append("Scripted openers")
        elif flexibility_score >= 60:
            identity_tags.append("Flexible routes")
        if adaptation_score >= 60:
            identity_tags.append("Pivot-ready")
        if not identity_tags:
            identity_tags.append("Generalist")

        if predictability_score >= 70:
            predictability_label = "High"
        elif predictability_score >= 45:
            predictability_label = "Medium"
        else:
            predictability_label = "Low"

        if flexibility_score >= 70:
            flexibility_label = "High"
        elif flexibility_score >= 45:
            flexibility_label = "Medium"
        else:
            flexibility_label = "Low"

        identity_label = " / ".join(identity_tags[:3])
        identity_summary_bits = []
        if top_comp.get("heroes"):
            identity_summary_bits.append(
                "Top shell: " + ", ".join((top_comp.get("heroes") or [])[:4])
            )
        if top_ban.get("hero"):
            identity_summary_bits.append(
                f"leans on {top_ban['hero']} bans"
            )
        if top_protect.get("hero"):
            identity_summary_bits.append(
                f"protects {top_protect['hero']} early"
            )

        win_condition_rows = []
        if top_comp.get("heroes"):
            win_condition_rows.append(
                f"Force them off {', '.join((top_comp.get('heroes') or [])[:4])}; that shell shows up in {top_comp.get('rate', 0)}% of tracked comps."
            )
        if volatile_hero_rows:
            volatile = volatile_hero_rows[0]
            if volatile.get("hero"):
                win_condition_rows.append(
                    f"Deny {volatile['hero']} when possible; their win rate swings {volatile.get('delta', 0)} pts when that hero is active."
                )
        if map_draft_rows:
            map_row = map_draft_rows[0]
            if map_row.get("map_name"):
                win_condition_rows.append(
                    f"On {map_row['map_name']}, expect {map_row.get('top_ban_hero') or 'their usual bans'} and {map_row.get('top_protect_hero') or 'their usual protects'} first."
                )

        return {
            "identity_label": identity_label,
            "identity_summary": "; ".join(identity_summary_bits) if identity_summary_bits else f"{team_name} drafts without a single dominant pattern.",
            "predictability_score": predictability_score,
            "predictability_label": predictability_label,
            "flexibility_score": flexibility_score,
            "flexibility_label": flexibility_label,
            "adaptation_score": adaptation_score,
            "opening_bans": opening_rows.get("ban1_rows", []),
            "opening_protects": opening_rows.get("protect1_rows", []),
            "top_comps": comp_rows[:6],
            "comfort_core_rows": comfort_core_rows[:5],
            "hero_pool_rows": hero_pool_rows,
            "player_hero_rows": player_hero_rows,
            "ban_line_rows": ban_line_rows[:10],
            "map_tendencies": map_draft_rows[:4],
            "mode_comp_rows": mode_comp_rows[:4],
            "first_phase_routes": most_likely_ban_route_rows[:4],
            "protect_reactions": protect1_influence_rows[:4],
            "pivot_rows": pivot_rows[:4],
            "comp_path_rows": comp_path_rows[:4],
            "equivalent_path_rows": equivalent_path_rows[:4],
            "volatile_hero_rows": volatile_hero_rows[:4],
            "map_state_rows": map_state_rows[:4],
            "win_condition_rows": win_condition_rows[:3],
        }

    def _build_draft_scout_ai_urls(opponent_id: int, opponent_name: str, draft_scout: dict) -> dict:
        top_comp = (draft_scout.get("top_comps") or [{}])[0]
        opening_ban = (draft_scout.get("opening_bans") or [{}])[0]
        opening_protect = (draft_scout.get("opening_protects") or [{}])[0]
        top_map = (draft_scout.get("map_tendencies") or [{}])[0]
        context_bits = [
            f"Identity: {draft_scout.get('identity_label', 'Unknown')}",
            f"Predictability: {draft_scout.get('predictability_score', 0)}/100",
            f"Flexibility: {draft_scout.get('flexibility_score', 0)}/100",
        ]
        if top_comp.get("heroes"):
            context_bits.append("Top shell: " + ", ".join((top_comp.get("heroes") or [])[:4]))
        if opening_ban.get("hero"):
            context_bits.append(f"Ban 1 priority: {opening_ban['hero']} ({opening_ban.get('rate', 0)}%)")
        if opening_protect.get("hero"):
            context_bits.append(f"Protect 1 priority: {opening_protect['hero']} ({opening_protect.get('rate', 0)}%)")
        if top_map.get("map_name"):
            context_bits.append(
                f"Map tendency: {top_map['map_name']} -> ban {top_map.get('top_ban_hero') or '-'}, protect {top_map.get('top_protect_hero') or '-'}"
            )
        draft_context = " | ".join(bit for bit in context_bits if bit)

        overview_prompt = (
            f"Scout {opponent_name} against us ({our_team['name']}) using only draft data, bans, comps, maps, and match scores. {draft_context}. "
            "Format the answer as labeled sections: Team identity, Most played comps, Ban behavior, Opening priorities, Flexibility and predictability, Predicted win conditions, Action plan. "
            "Be concrete and exploit-focused."
        )
        routes_prompt = (
            f"Break down {opponent_name}'s opening draft routes against us ({our_team['name']}). {draft_context}. "
            "Explain their Ban 1 and Protect 1 priorities, the most likely follow-up ban/protect route, and the cleanest counter-route for us."
        )
        map_prompt = (
            f"Build a map-specific prep plan for {opponent_name} against us ({our_team['name']}). {draft_context}. "
            "For each relevant map or mode, explain what they tend to ban, what they protect, which comp shell they prefer, and what our ban/protect response should be."
        )
        map_plan_urls = {}
        for map_row in (draft_scout.get("map_tendencies") or [])[:4]:
            map_name = map_row.get("map_name")
            if not map_name:
                continue
            specific_map_prompt = (
                f"Expand the {map_name} prep plan for {opponent_name} against us ({our_team['name']}). {draft_context}. "
                f"On this map, their likely first ban is {map_row.get('top_ban_hero') or '-'} at {map_row.get('top_ban_rate', 0)}%, "
                f"and their likely first protect is {map_row.get('top_protect_hero') or '-'} at {map_row.get('top_protect_rate', 0)}%. "
                "Give the exact ban/protect response, expected comp shell, danger picks, and our win condition."
            )
            map_plan_urls[map_name] = _build_ai_url(opponent_id, opponent_name, specific_map_prompt)
        predictability_prompt = (
            f"Assess how predictable and adaptable {opponent_name} is in draft against us ({our_team['name']}). {draft_context}. "
            "Explain where their draft is scripted, where they can pivot, which heroes or comps indicate the pivot, and how we should punish low-flex branches."
        )
        comp_shells_prompt = (
            f"Expand {opponent_name}'s expected comp shells against us ({our_team['name']}). {draft_context}. "
            "For the top six shells, explain the likely game plan, key heroes, map fit, draft tells, and the cleanest ban/protect response."
        )
        win_conditions_prompt = (
            f"Turn this scouting data into concrete win conditions for us against {opponent_name}. {draft_context}. "
            "Prioritize what to deny first, what shell to break, what map states to force, and what mistakes we should bait."
        )
        player_pools_prompt = (
            f"Analyze {opponent_name}'s player hero pools against us ({our_team['name']}). {draft_context}. "
            "Identify comfort threats, targetable hero pools, likely swaps, and which player-hero pair should shape our bans."
        )
        sample_warning_prompt = (
            f"Audit the sample-size risk in this {opponent_name} scouting report against us ({our_team['name']}). {draft_context}. "
            "Call out which reads are reliable, which are low sample, and how coaches should weight the signals."
        )

        return {
            "overview": _build_ai_url(opponent_id, opponent_name, overview_prompt),
            "routes": _build_ai_url(opponent_id, opponent_name, routes_prompt),
            "map_plan": _build_ai_url(opponent_id, opponent_name, map_prompt),
            "map_plans": map_plan_urls,
            "predictability": _build_ai_url(opponent_id, opponent_name, predictability_prompt),
            "comp_shells": _build_ai_url(opponent_id, opponent_name, comp_shells_prompt),
            "win_conditions": _build_ai_url(opponent_id, opponent_name, win_conditions_prompt),
            "player_pools": _build_ai_url(opponent_id, opponent_name, player_pools_prompt),
            "sample_warning": _build_ai_url(opponent_id, opponent_name, sample_warning_prompt),
        }

    scout_rows: list[dict] = []
    for row in opponent_rows:
        opponent_id = int(row["id"])
        opponent_name = (row["name"] or "").strip()
        combined_summary = _build_scout_summary(filtered_history, opponent_id, opponent_name)
        if not combined_summary:
            continue

        scrim_summary = _build_scout_summary(filtered_scrim_history, opponent_id, opponent_name)

        opponent_tournament_history = build_team_tournament_scrims({"id": opponent_id, "name": opponent_name})
        opponent_tournament_history = filter_scrims_by_season(opponent_tournament_history, selected_season)
        opponent_tournament_history = _filter_scrims_to_map(opponent_tournament_history, selected_map_name)
        tournament_summary = _build_scout_summary(
            opponent_tournament_history,
            opponent_id,
            opponent_name,
            include_all_matches=True,
        )

        source_compare = [
            {
                "label": "Scrims",
                "maps": int((scrim_summary or {}).get("maps") or 0),
                "win_rate": float((scrim_summary or {}).get("win_rate") or 0.0),
                "favorite_comp": ((scrim_summary or {}).get("favorite_comps") or [{}])[0],
                "top_ban": ((scrim_summary or {}).get("recommended_bans") or [{}])[0],
            },
            {
                "label": "Tournaments",
                "maps": int((tournament_summary or {}).get("maps") or 0),
                "win_rate": float((tournament_summary or {}).get("win_rate") or 0.0),
                "favorite_comp": ((tournament_summary or {}).get("favorite_comps") or [{}])[0],
                "top_ban": ((tournament_summary or {}).get("recommended_bans") or [{}])[0],
            },
        ]

        overview_prompt = (
            f"Scout {opponent_name} against us ({our_team['name']}). Use {filter_context}. "
            "Format the answer as labeled sections: Overview, Favorite comps, Best bans against them, "
            "Bans hurting us most, First ban recommendation, Contingency plan. "
            "For the contingency plan, explicitly name the most likely enemy pivot after our first ban and the exact hero or comp adjustment we should make in response."
        )
        favorite_comps_prompt = (
            f"Expand on {opponent_name}'s favorite comps against us ({our_team['name']}). Use {filter_context}. "
            "Explain what their most common comp shells are trying to do, what map or fight conditions they prefer, and which ban best disrupts each shell."
        )
        best_bans_prompt = (
            f"Expand on the best bans into {opponent_name} against us ({our_team['name']}). Use {filter_context}. "
            "Rank the top ban targets, explain why each one matters, and state what enemy comp or pivot remains if we remove the first target."
        )
        hurt_us_prompt = (
            f"Expand on which {opponent_name} bans hurt us most. Use {filter_context}. "
            "Explain why each ban damages our plan, which of our players or comp shells lose the most value, and what safer fallback we should move to."
        )
        source_compare_prompt = (
            f"Compare {opponent_name}'s scrim data versus tournament data against us ({our_team['name']}). Use {filter_context}. "
            "Explain what changes between scrims and tournaments in their favorite comps, ban priorities, and our results, and tell us which source should drive prep."
        )
        ban_correlation_prompt = (
            f"Compare our bans versus tournament-field bans against {opponent_name}. Use {filter_context}. "
            "Show which heroes we already align on, which high-value bans the tournament field prioritizes that we are missing, and what our first two-ban plan should be."
        )

        ban_correlation = _build_ban_correlation_rows(opponent_id, opponent_name)

        scout_rows.append(
            {
                "team_id": opponent_id,
                "team_name": opponent_name,
                "logo_path": row["logo_path"],
                "quality_tag": (row["quality_tag"] or "").strip(),
                "maps": combined_summary["maps"],
                "wins": combined_summary["wins"],
                "losses": combined_summary["losses"],
                "win_rate": combined_summary["win_rate"],
                "recommended_bans": combined_summary["recommended_bans"],
                "top_enemy_bans": combined_summary["top_enemy_bans"],
                "favorite_comps": combined_summary["favorite_comps"],
                "source_compare": source_compare,
                "ban_correlation": ban_correlation,
                "ai_url": _build_ai_url(opponent_id, opponent_name, overview_prompt),
                "ai_urls": {
                    "overview": _build_ai_url(opponent_id, opponent_name, overview_prompt),
                    "favorite_comps": _build_ai_url(opponent_id, opponent_name, favorite_comps_prompt),
                    "recommended_bans": _build_ai_url(opponent_id, opponent_name, best_bans_prompt),
                    "top_enemy_bans": _build_ai_url(opponent_id, opponent_name, hurt_us_prompt),
                    "source_compare": _build_ai_url(opponent_id, opponent_name, source_compare_prompt),
                    "ban_correlation": _build_ai_url(opponent_id, opponent_name, ban_correlation_prompt),
                },
            }
        )

    scout_rows.sort(
        key=lambda row: (
            row["maps"],
            row["wins"] + row["losses"],
            row["team_name"].lower(),
        ),
        reverse=True,
    )

    try:
        selected_team_id = int(request.args.get("opponent_id") or 0)
    except (TypeError, ValueError):
        selected_team_id = 0

    selected_scout_row = None
    if scout_rows:
        selected_scout_row = next(
            (row for row in scout_rows if int(row["team_id"]) == selected_team_id),
            scout_rows[0],
        )
        selected_team_id = int(selected_scout_row["team_id"])

    selected_scout_detail = {}
    if selected_scout_row is not None:
        scout_team = {
            "id": int(selected_scout_row["team_id"]),
            "name": (selected_scout_row["team_name"] or "").strip(),
        }
        scout_scrims = get_scrims_for_team(scout_team["id"], scout_team["name"])
        scout_tournaments = build_team_tournament_scrims(scout_team)
        scout_history = scout_scrims + scout_tournaments
        scout_history = filter_scrims_by_season(scout_history, selected_season)
        scout_history = _filter_scrims_to_map(scout_history, selected_map_name)

        scout_player_rows = db.execute(
            "SELECT id, name, role, main_hero, notes, is_sub FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
            (scout_team["id"],),
        ).fetchall()
        scout_players = [
            {
                "id": int(row["id"]),
                "name": (row["name"] or "").strip(),
                "role": (row["role"] or "").strip(),
                "main_hero": (row["main_hero"] or "").strip(),
                "notes": (row["notes"] or "").strip(),
                "is_sub": bool(row["is_sub"]),
            }
            for row in scout_player_rows
        ]
        scout_roster_names = [player["name"] for player in scout_players if player["name"]]
        scout_analytics = build_scrim_analytics(scout_history, roster_player_names=scout_roster_names)
        scout_map_overview = build_team_map_overview(
            scout_history,
            map_modes=MAP_MODES,
            map_types=MAP_TYPES,
            normalize_map_type_value=normalize_map_type_value,
            get_map_outcome_for_slot=get_map_outcome_for_slot,
            get_map_image_url=get_map_image_url,
        )
        scout_hero_profile = build_team_hero_profile(
            scout_history,
            scout_players,
            team_slots=TEAM_SLOTS,
            canonical_draft_hero=_canonical_draft_hero,
            get_map_outcome_for_slot=get_map_outcome_for_slot,
        )
        scout_hero_rows = scout_hero_profile.get("hero_rows", [])
        scout_comp_rows = scout_hero_profile.get("comp_rows", [])
        scout_attack_defense = build_atk_def_wr(scout_history, attack_defense_maps=ATTACK_DEFENSE_MAPS)
        scout_pivot = build_pivot_wr(scout_history, attack_defense_maps=ATTACK_DEFENSE_MAPS)
        scout_ban_impact = build_team_ban_impact(scout_history)
        scout_draft_correlation = build_prep_draft_correlation_bundle(scout_history)
        scout_expected_plan = build_prep_expected_comp_plan(scout_history, scout_player_rows, scout_analytics, all_scrims=scout_history)
        scout_tree_model = build_opponent_tree_model(scout_history, scout_history)
        draft_scout = _build_draft_scout_bundle(scout_team["name"], scout_history, scout_analytics, scout_tree_model)
        scout_roster_lookup = {name.strip().lower() for name in scout_roster_names if name and name.strip()}
        if scout_roster_lookup:
            draft_scout["player_hero_rows"] = [
                row
                for row in (draft_scout.get("player_hero_rows") or [])
                if (row.get("player") or "").strip().lower() in scout_roster_lookup
            ]
        draft_scout_ai_urls = _build_draft_scout_ai_urls(scout_team["id"], scout_team["name"], draft_scout)
        scout_top_hero_rows = scout_hero_rows[:3]
        scout_open_impact_rows = []
        for hero_row in scout_top_hero_rows:
            hero_name = hero_row.get("hero", "")
            if not hero_name:
                continue
            hero_detail = build_team_hero_insights(scout_history, hero_name)
            ban_impact = hero_detail.get("ban_impact", {}) if isinstance(hero_detail, dict) else {}
            scout_open_impact_rows.append(
                {
                    "hero": hero_name,
                    "maps": hero_detail.get("summary", {}).get("maps_played", 0),
                    "win_rate": hero_detail.get("summary", {}).get("win_rate", 0),
                    "open_vs_banned_delta": ban_impact.get("open_vs_banned_delta"),
                    "open_wr": ban_impact.get("win_rate_when_open"),
                    "banned_wr": ban_impact.get("win_rate_when_banned"),
                    "top_pivot": (ban_impact.get("top_pivots") or [{}])[0],
                }
            )

        selected_scout_detail = {
            "hero_rows": scout_hero_rows[:6],
            "comp_rows": scout_comp_rows[:4],
            "map_rows": (scout_map_overview.get("team_map_cards") or [])[:4],
            "mode_rows": (scout_map_overview.get("team_map_mode_rows") or [])[:3],
            "atk_def_rows": (scout_attack_defense.get("per_map") or [])[:4],
            "pivot_rows": (scout_pivot.get("per_player") or [])[:4],
            "ban_impact_rows": scout_ban_impact[:5],
            "ban_pairs": (scout_draft_correlation.get("ban") or {}).get("cooccurrence_rows", [])[:5],
            "protect_pairs": (scout_draft_correlation.get("protect") or {}).get("cooccurrence_rows", [])[:5],
            "expected_comp_variants": scout_expected_plan.get("expected_comp_variants", [])[:3],
            "four_hero_combos": scout_expected_plan.get("four_hero_combos", [])[:4],
            "suggested_adjustments": scout_expected_plan.get("suggested_adjustments", [])[:5],
            "hero_player_differences": scout_expected_plan.get("hero_player_differences", [])[:4],
            "open_hero_rows": scout_open_impact_rows,
            "draft_scout": draft_scout,
            "ai_urls": draft_scout_ai_urls,
            "overview": scout_analytics,
        }

    try:
        selected_prep_team_id = int(request.args.get("prep_team_id") or our_team["id"])
    except (TypeError, ValueError):
        selected_prep_team_id = our_team["id"]

    prep_team_row = next((row for row in team_rows if int(row["id"]) == selected_prep_team_id), None)
    if prep_team_row is None:
        prep_team_row = next((row for row in team_rows if int(row["id"]) == our_team["id"]), None)
    if prep_team_row is None:
        prep_team_row = team_rows[0] if team_rows else None

    prep_team = None
    prep_html = ""
    prep_team_rows = [dict(row) for row in team_rows]
    prep_selected_map_type = get_selected_map_type(request.args.get("prep_map_type", "all"))
    prep_selected_enemy_id = (request.args.get("prep_enemy_id", "") or "").strip()
    if prep_team_row is not None:
        prep_team = {"id": int(prep_team_row["id"]), "name": (prep_team_row["name"] or "").strip()}
        prep_scrims_all = get_scrims_for_team(prep_team["id"], prep_team["name"])
        prep_season_options = get_scrim_season_options(prep_scrims_all)
        prep_has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in prep_scrims_all)
        prep_default_season = get_current_season_from_recent_scrim(prep_scrims_all)
        prep_selected_season = get_selected_season(
            request.args.get("prep_season", ""),
            prep_season_options,
            allow_unspecified=prep_has_unseasoned_scrims,
            default_season=prep_default_season,
        )
        prep_scrims = filter_scrims_by_season(prep_scrims_all, prep_selected_season)
        prep_scrims = filter_scrims_by_map_type(prep_scrims, prep_selected_map_type)
        prep_enemy_rows = [
            {
                "id": int(row["id"]),
                "name": (row["name"] or "").strip(),
                "logo_path": row["logo_path"],
                "quality_tag": (row["quality_tag"] or "").strip(),
                "is_personal": bool(row["is_personal"]),
            }
            for row in team_rows
            if int(row["id"]) != prep_team["id"]
        ]
        prep_players = db.execute(
            "SELECT id, name, role, main_hero, notes, is_sub FROM players WHERE team_id = ? ORDER BY name COLLATE NOCASE",
            (prep_team["id"],),
        ).fetchall()
        prep_context = build_team_prep_context(
            team_scrims=prep_scrims,
            team_players=prep_players,
            enemy_teams=prep_enemy_rows,
            selected_enemy_id_raw=prep_selected_enemy_id,
            compare_map_a_raw=request.args.get("prep_compare_map_a", ""),
            compare_map_b_raw=request.args.get("prep_compare_map_b", ""),
        )
        prep_html = render_template(
            "_team_prep_content.html",
            team=prep_team,
            enemy_teams=prep_enemy_rows,
            selected_season=prep_selected_season,
            selected_map_type=prep_selected_map_type,
            **prep_context,
        )
    else:
        prep_season_options = season_options
        prep_has_unseasoned_scrims = has_unseasoned_scrims
        prep_selected_season = get_selected_season(
            request.args.get("prep_season", ""),
            prep_season_options,
            allow_unspecified=prep_has_unseasoned_scrims,
            default_season=default_season,
        )

    return render_template(
        "team_scouting.html",
        our_team=our_team,
        scout_rows=scout_rows,
        selected_scout_row=selected_scout_row,
        selected_team_id=selected_team_id,
        selected_season=selected_season,
        season_options=season_options,
        has_unseasoned_scrims=has_unseasoned_scrims,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
        selected_map_name=selected_map_name,
        map_options=map_options,
        min_maps=min_maps,
        prep_team_rows=prep_team_rows,
        selected_prep_team_id=selected_prep_team_id,
        selected_prep_season=prep_selected_season,
        selected_prep_map_type=prep_selected_map_type,
        selected_prep_enemy_id=prep_selected_enemy_id,
        prep_season_options=prep_season_options,
        prep_has_unseasoned_scrims=prep_has_unseasoned_scrims,
        prep_html=prep_html,
        selected_scout_detail=selected_scout_detail,
        get_map_image_url=get_map_image_url,
    )


def _ensure_team_sort_order_column(db: sqlite3.Connection) -> None:
    team_columns = {row[1] for row in db.execute("PRAGMA table_info(teams)").fetchall()}
    if "sort_order" not in team_columns:
        db.execute("ALTER TABLE teams ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
        db.commit()


@app.route("/teams")
def teams():
    db = get_db()
    migrate_enemy_teams_to_team_database(db)
    _ensure_team_sort_order_column(db)
    personal_rows = db.execute(
        "SELECT id, name FROM teams WHERE is_personal = 1 ORDER BY COALESCE(sort_order, 0), name COLLATE NOCASE"
    ).fetchall()
    if len(personal_rows) > 1:
        keep_personal_id = int(personal_rows[0]["id"])
        db.execute("UPDATE teams SET is_personal = 0 WHERE id != ?", (keep_personal_id,))
        db.commit()
        personal_rows = [personal_rows[0]]
    personal_team = personal_rows[0] if personal_rows else None

    selected_sort = (request.args.get("sort") or "quality").strip().lower()
    if selected_sort == "custom":
        selected_sort = "quality"
    if selected_sort not in {"quality", "win_rate", "last_played", "name"}:
        selected_sort = "quality"
    selected_view = (request.args.get("view") or "rows").strip().lower()
    if selected_view not in {"rows", "boxes"}:
        selected_view = "rows"
    team_rows = db.execute(
        """
        SELECT
            t.id,
            t.name,
            t.notes,
            t.quality_tag,
            COALESCE(t.sort_order, 0) AS sort_order,
            t.logo_path,
            t.is_personal,
            COUNT(p.id) AS player_count
        FROM teams t
        LEFT JOIN players p ON p.team_id = t.id
        GROUP BY t.id
        ORDER BY COALESCE(t.sort_order, 0), t.name COLLATE NOCASE
        """
    ).fetchall()
    team_ids = [int(row["id"]) for row in team_rows]
    roster_rows_by_team: dict[int, list] = {team_id: [] for team_id in team_ids}
    if team_ids:
        placeholders = ",".join("?" for _ in team_ids)
        roster_rows = db.execute(
            f"""
            SELECT team_id, name, role, COALESCE(is_sub, 0) AS is_sub
            FROM players
            WHERE team_id IN ({placeholders})
            ORDER BY
                team_id,
                CASE
                    WHEN role = 'Coach' THEN 10
                    WHEN role = 'AC' THEN 11
                    WHEN role = 'Analyst' THEN 12
                    WHEN COALESCE(is_sub, 0) = 0 THEN 0
                    ELSE 1
                END,
                name COLLATE NOCASE
            """,
            team_ids,
        ).fetchall()
        for player in roster_rows:
            roster_rows_by_team.setdefault(int(player["team_id"]), []).append(player)

    season_options = get_scrim_season_options(SCRIMS)
    default_season = get_current_season_from_recent_scrim(SCRIMS)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in SCRIMS)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )

    teams_with_scrim_stats = []
    staff_roles = {"Coach", "AC", "Analyst"}
    quality_rank = {"Preferred": 0, "Semi Preferred": 1, "Good": 2, "Avoid": 3}
    team_scrims_cache: dict[int, list[dict]] = {}

    def _team_scrims_for(row) -> list[dict]:
        team_id = int(row["id"])
        if team_id not in team_scrims_cache:
            team_scrims_cache[team_id] = get_scrims_for_team(team_id, row["name"])
        return team_scrims_cache[team_id]

    personal_team_scrims = []
    if personal_team is not None:
        personal_team_scrims = filter_scrims_by_season(
            _team_scrims_for(personal_team),
            selected_season,
        )
    for row in team_rows:
        all_team_scrims = _team_scrims_for(row)
        team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
        stats_scrims = team_scrims
        if personal_team is not None and int(row["id"]) != int(personal_team["id"]):
            stats_scrims = [
                scrim for scrim in personal_team_scrims
                if scrim_involves_team(scrim, int(row["id"]), row["name"])
            ]
        team_maps = sum(len(scrim.get("maps", [])) for scrim in stats_scrims)
        team_wins = sum(
            1
            for scrim in stats_scrims
            for map_entry in scrim.get("maps", [])
            if get_map_outcome_for_slot(map_entry, map_entry.get("our_team_slot", "team1")) == "Win"
        )
        team_win_rate = round((team_wins / team_maps) * 100, 1) if team_maps else 0
        if team_maps <= 0:
            win_rate_class = "wr-empty"
        elif team_win_rate >= 60:
            win_rate_class = "wr-good"
        elif team_win_rate >= 45:
            win_rate_class = "wr-ok"
        else:
            win_rate_class = "wr-bad"

        dated_scrims = []
        for scrim in stats_scrims:
            parsed_date = _parse_scrim_date(scrim.get("scrim_date", ""))
            if parsed_date is not None:
                dated_scrims.append((parsed_date, scrim.get("scrim_date", "")))
        last_played = ""
        last_played_sort = ""
        if dated_scrims:
            latest_date, latest_raw = max(dated_scrims, key=lambda item: item[0])
            last_played = latest_date.strftime("%m/%d/%Y")
            last_played_sort = latest_date.isoformat()

        roster_rows = roster_rows_by_team.get(int(row["id"]), [])
        active_roster = [
            {"name": (player["name"] or "").strip(), "role": (player["role"] or "").strip(), "is_sub": bool(player["is_sub"])}
            for player in roster_rows
            if (player["name"] or "").strip() and (player["role"] or "").strip() not in staff_roles
        ]
        roster_by_role = {
            "Vanguard": [player for player in active_roster if player["role"] == "Vanguard"],
            "Duelist": [player for player in active_roster if player["role"] == "Duelist"],
            "Strategist": [player for player in active_roster if player["role"] == "Strategist"],
            "Flex / Other": [player for player in active_roster if player["role"] not in {"Vanguard", "Duelist", "Strategist"}],
        }
        staff = [
            {"name": (player["name"] or "").strip(), "role": (player["role"] or "").strip()}
            for player in roster_rows
            if (player["name"] or "").strip() and (player["role"] or "").strip() in staff_roles
        ]

        teams_with_scrim_stats.append(
            {
                "id": row["id"],
                "name": row["name"],
                "notes": row["notes"],
                "quality_tag": row["quality_tag"],
                "quality_rank": quality_rank.get(row["quality_tag"], 99),
                "sort_order": int(row["sort_order"] or 0),
                "logo_path": row["logo_path"],
                "is_personal": bool(row["is_personal"]),
                "player_count": row["player_count"],
                "active_roster": active_roster,
                "roster_by_role": roster_by_role,
                "active_roster_count": len(active_roster),
                "staff": staff,
                "scrim_count": len(team_scrims),
                "stats_context": f"Our WR vs {row['name']}" if personal_team is not None and int(row["id"]) != int(personal_team["id"]) else "Overall",
                "map_count": team_maps,
                "map_win_rate": team_win_rate,
                "win_rate_class": win_rate_class,
                "last_played": last_played,
                "last_played_sort": last_played_sort,
            }
        )

    if selected_sort == "win_rate":
        teams_with_scrim_stats.sort(key=lambda team: (team["map_count"] > 0, team["map_win_rate"], team["map_count"], team["name"].lower()), reverse=True)
    elif selected_sort == "last_played":
        teams_with_scrim_stats.sort(key=lambda team: (team["last_played_sort"], team["name"].lower()), reverse=True)
    elif selected_sort == "name":
        teams_with_scrim_stats.sort(key=lambda team: team["name"].lower())
    else:
        teams_with_scrim_stats.sort(key=lambda team: (team["quality_rank"], team["map_count"] <= 0, team["map_win_rate"], team["sort_order"], team["name"].lower()))

    # Personal team always floats to the top regardless of sort
    teams_with_scrim_stats.sort(key=lambda t: not t["is_personal"])

    personal_teams = [team for team in teams_with_scrim_stats if team["is_personal"]]
    chart_teams = [
        {
            "name": team["name"],
            "map_count": team["map_count"],
            "map_win_rate": team["map_win_rate"],
        }
        for team in teams_with_scrim_stats
        if team["map_count"]
    ]

    return render_template(
        "teams.html",
        teams=teams_with_scrim_stats,
        personal_teams=personal_teams,
        chart_teams=chart_teams,
        season_options=season_options,
        selected_season=selected_season,
        selected_sort=selected_sort,
        selected_view=selected_view,
        has_unseasoned_scrims=has_unseasoned_scrims,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
    )


@app.route("/teams/compare")
def teams_compare():
    db = get_db()
    team_rows = db.execute(
        "SELECT id, name, notes, logo_path, is_personal FROM teams ORDER BY name COLLATE NOCASE"
    ).fetchall()
    team_options = [dict(row) for row in team_rows]
    team_lookup = {str(row["id"]): dict(row) for row in team_rows}

    selected_team_a_id = (request.args.get("team_a") or "").strip()
    selected_team_b_id = (request.args.get("team_b") or "").strip()
    selected_mode = (request.args.get("mode") or "scrims").strip().lower()
    if selected_mode not in {"scrims", "tournaments"}:
        selected_mode = "scrims"

    def _ban_rate_map(analytics: dict) -> dict[str, float]:
        return {row.get("hero", ""): float(row.get("ban_rate", 0)) for row in analytics.get("ban_rows", []) if row.get("hero")}

    def _protect_rate_map(analytics: dict) -> dict[str, float]:
        return {row.get("hero", ""): float(row.get("protect_rate", 0)) for row in analytics.get("protect_rows", []) if row.get("hero")}

    def _open_rows_map(analytics: dict) -> dict[str, dict]:
        return {row.get("hero", ""): row for row in analytics.get("hero_open_rows", []) if row.get("hero")}

    def load_team_payload(team_row: dict | None) -> dict | None:
        if team_row is None:
            return None

        scrim_pool = get_scrims_for_team(team_row["id"], team_row["name"])
        tournament_pool = build_team_tournament_scrims(team_row)
        source_pool = tournament_pool if selected_mode == "tournaments" else scrim_pool
        latest_season = get_current_season_from_recent_scrim(source_pool)
        team_scrims = filter_scrims_by_season(source_pool, latest_season)
        analytics = build_scrim_analytics(team_scrims)
        return {
            "team": team_row,
            "analytics": analytics,
            "top_heroes": analytics.get("hero_rows", [])[:8],
            "top_maps": analytics.get("map_rows", [])[:8],
            "all_heroes": analytics.get("hero_rows", []),
            "all_maps": analytics.get("map_rows", []),
            "latest_season": latest_season,
            "ban_rate_map": _ban_rate_map(analytics),
            "protect_rate_map": _protect_rate_map(analytics),
            "open_rows_map": _open_rows_map(analytics),
            "flow_rows": analytics.get("ban_next_rows", [])[:6],
        }

    team_a = load_team_payload(team_lookup.get(selected_team_a_id))
    team_b = load_team_payload(team_lookup.get(selected_team_b_id))

    ban_matchup_rows = []
    shared_heroes_rows = []
    shared_maps_rows = []
    if team_a and team_b:
        # Shared hero WR comparison
        a_hero_map = {r["hero"]: r for r in team_a["all_heroes"]}
        b_hero_map = {r["hero"]: r for r in team_b["all_heroes"]}
        for hero, a_row in a_hero_map.items():
            if hero in b_hero_map:
                b_row = b_hero_map[hero]
                shared_heroes_rows.append({
                    "hero": hero,
                    "a_maps": a_row["maps"],
                    "a_wr": a_row["win_rate"],
                    "b_maps": b_row["maps"],
                    "b_wr": b_row["win_rate"],
                    "wr_diff": round(a_row["win_rate"] - b_row["win_rate"], 1),
                })
        shared_heroes_rows.sort(key=lambda r: r["a_maps"] + r["b_maps"], reverse=True)

        # Shared map WR comparison
        a_map_map = {r["map_name"]: r for r in team_a["all_maps"]}
        b_map_map = {r["map_name"]: r for r in team_b["all_maps"]}
        for map_name, a_row in a_map_map.items():
            if map_name in b_map_map:
                b_row = b_map_map[map_name]
                shared_maps_rows.append({
                    "map_name": map_name,
                    "a_maps": a_row["maps"],
                    "a_wr": a_row["win_rate"],
                    "b_maps": b_row["maps"],
                    "b_wr": b_row["win_rate"],
                    "wr_diff": round(a_row["win_rate"] - b_row["win_rate"], 1),
                })
        shared_maps_rows.sort(key=lambda r: r["a_maps"] + r["b_maps"], reverse=True)

        hero_candidates = []
        hero_candidates.extend([row.get("hero", "") for row in team_a["analytics"].get("ban_rows", [])[:8]])
        hero_candidates.extend([row.get("hero", "") for row in team_b["analytics"].get("ban_rows", [])[:8]])

        seen = set()
        ordered_heroes = []
        for hero in hero_candidates:
            if not hero or hero in seen:
                continue
            seen.add(hero)
            ordered_heroes.append(hero)

        for hero in ordered_heroes:
            a_ban_rate = float(team_a["ban_rate_map"].get(hero, 0))
            b_ban_rate = float(team_b["ban_rate_map"].get(hero, 0))
            a_protect_rate = float(team_a["protect_rate_map"].get(hero, 0))
            b_protect_rate = float(team_b["protect_rate_map"].get(hero, 0))
            a_open_row = team_a["open_rows_map"].get(hero, {})
            b_open_row = team_b["open_rows_map"].get(hero, {})
            ban_matchup_rows.append(
                {
                    "hero": hero,
                    "a_ban_rate": round(a_ban_rate, 1),
                    "b_ban_rate": round(b_ban_rate, 1),
                    "ban_rate_diff": round(a_ban_rate - b_ban_rate, 1),
                    "a_protect_rate": round(a_protect_rate, 1),
                    "b_protect_rate": round(b_protect_rate, 1),
                    "protect_rate_diff": round(a_protect_rate - b_protect_rate, 1),
                    "a_open_wr": a_open_row.get("win_rate_when_open"),
                    "b_open_wr": b_open_row.get("win_rate_when_open"),
                    "a_banned_wr": a_open_row.get("win_rate_when_banned"),
                    "b_banned_wr": b_open_row.get("win_rate_when_banned"),
                }
            )

    return render_template(
        "teams_compare.html",
        team_options=team_options,
        selected_team_a_id=selected_team_a_id,
        selected_team_b_id=selected_team_b_id,
        selected_mode=selected_mode,
        team_a=team_a,
        team_b=team_b,
        ban_matchup_rows=ban_matchup_rows,
        shared_heroes_rows=shared_heroes_rows,
        shared_maps_rows=shared_maps_rows,
        map_images=MAP_IMAGES,
    )


@app.route("/teams/create", methods=["POST"])
def create_team():
    db = get_db()
    _ensure_team_sort_order_column(db)
    raw_name = request.form.get("name", "")
    name = normalize_player_name(raw_name)
    notes = request.form.get("notes", "").strip()
    quality_tag_raw = " ".join(request.form.get("quality_tag", "").strip().split())[:32]
    quality_tag = quality_tag_raw if quality_tag_raw in TEAM_QUALITY_TAG_OPTIONS else ""
    try:
        sort_order = int(request.form.get("sort_order", "0") or 0)
    except ValueError:
        sort_order = 0
    logo_path = save_team_logo(request.files.get("logo"), name)
    is_personal = 1 if request.form.get("is_personal", "").strip() == "1" else 0

    if not name:
        flash("Team name is required.", "error")
        return redirect(url_for("teams"))

    try:
        if is_personal:
            db.execute("UPDATE teams SET is_personal = 0")
        db.execute(
            "INSERT INTO teams (name, notes, quality_tag, sort_order, logo_path, is_personal) VALUES (?, ?, ?, ?, ?, ?)",
            (name, notes, quality_tag, sort_order, logo_path, is_personal),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash("A team with that name already exists.", "error")
        return redirect(url_for("teams"))

    flash("Team created.", "success")
    return redirect(url_for("teams"))


@app.route("/teams/<int:team_id>/edit", methods=["POST"])
def edit_team(team_id: int):
    db = get_db()
    _ensure_team_sort_order_column(db)
    current = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if current is None:
        abort(404)

    raw_name = request.form.get("name", "")
    name = normalize_player_name(raw_name)
    notes = request.form.get("notes", "").strip()
    quality_tag_raw = " ".join(request.form.get("quality_tag", "").strip().split())[:32]
    quality_tag = quality_tag_raw if quality_tag_raw in TEAM_QUALITY_TAG_OPTIONS else ""
    current_sort_order = current["sort_order"] if "sort_order" in current.keys() else 0
    try:
        sort_order = int(request.form.get("sort_order", "0") or current_sort_order or 0)
    except ValueError:
        sort_order = 0
    remove_logo = request.form.get("remove_logo", "").strip() == "1"
    new_logo_path = save_team_logo(request.files.get("logo"), name)
    raw_personal = request.form.get("is_personal")
    if raw_personal is None:
        is_personal = int(current["is_personal"] or 0)
    else:
        is_personal = 1 if (raw_personal or "").strip() == "1" else 0
    if not name:
        flash("Team name is required.", "error")
        return redirect(url_for("team_detail", team_id=team_id))

    try:
        current_logo_path = current["logo_path"]
        logo_path = current_logo_path
        if new_logo_path:
            logo_path = new_logo_path
            if current_logo_path and current_logo_path != new_logo_path:
                delete_team_logo_file(current_logo_path)
        elif remove_logo and current_logo_path:
            logo_path = ""
            delete_team_logo_file(current_logo_path)
        if is_personal:
            db.execute("UPDATE teams SET is_personal = 0 WHERE id != ?", (team_id,))
        db.execute(
            "UPDATE teams SET name = ?, notes = ?, quality_tag = ?, sort_order = ?, logo_path = ?, is_personal = ? WHERE id = ?",
            (name, notes, quality_tag, sort_order, logo_path, is_personal, team_id),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash("A team with that name already exists.", "error")
        return redirect(url_for("team_detail", team_id=team_id))

    flash("Team updated.", "success")
    return redirect(url_for("team_detail", team_id=team_id))


@app.route("/teams/<int:team_id>/quick-access", methods=["POST"])
def toggle_team_quick_access(team_id: int):
    db = get_db()
    _ensure_team_sort_order_column(db)
    team = db.execute("SELECT id, is_personal FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    action = (request.form.get("action") or "toggle").strip().lower()
    current_value = 1 if team["is_personal"] else 0
    if action == "add":
        next_value = 1
    elif action == "remove":
        next_value = 0
    else:
        next_value = 0 if current_value else 1

    if next_value:
        db.execute("UPDATE teams SET is_personal = 0 WHERE id != ?", (team_id,))
    db.execute("UPDATE teams SET is_personal = ? WHERE id = ?", (next_value, team_id))
    db.commit()

    flash("Quick access updated.", "success")
    return redirect(url_for("teams", season=request.form.get("season", "all")))


@app.route("/teams/<int:team_id>/scrims.csv")
def team_scrims_csv(team_id: int):
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    all_team_scrims = get_scrims_for_team(team["id"], team["name"])
    season_options = get_scrim_season_options(all_team_scrims)
    default_season = get_current_season_from_recent_scrim(all_team_scrims)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in all_team_scrims)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )
    selected_map_type = get_selected_map_type(request.args.get("map_type", "all"))
    team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
    team_scrims = filter_scrims_by_map_type(team_scrims, selected_map_type)

    scrim_log = build_scrim_log_rows(
        team_scrims,
        target_team_id=team["id"],
        target_team_name=team["name"],
        team_slots=TEAM_SLOTS,
        canonical_draft_hero=_canonical_draft_hero,
        hero_match_key=_hero_match_key,
        hero_role=_hero_role,
        opposite_team_slot=opposite_team_slot,
        get_map_outcome_for_slot=get_map_outcome_for_slot,
        infer_result_from_score_text=infer_result_from_score_text,
        split_score_pair=split_score_pair,
    )
    filtered_rows = filter_scrim_log_rows(
        scrim_log["rows"],
        opponent=request.args.get("opponent", ""),
        map_name=request.args.get("map", ""),
        ban=request.args.get("ban", ""),
        duelist=request.args.get("duelist", ""),
    )

    filename_parts = [secure_filename((team["name"] or "team").strip()) or f"team-{team_id}", "scrims"]
    if selected_season and selected_season != "all":
        filename_parts.append(f"season-{selected_season}")
    if selected_map_type and selected_map_type != "all":
        filename_parts.append(secure_filename(selected_map_type.lower()))
    archive_bytes = build_scrim_log_export_archive(
        team["name"],
        filtered_rows,
        normalize_match_team_slot=normalize_match_team_slot,
        opposite_team_slot=opposite_team_slot,
        simulator_slot_order=SIMULATOR_SLOT_ORDER,
    )
    filename = "-".join(filename_parts) + ".zip"

    return Response(
        archive_bytes,
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/teams/<int:team_id>")
def team_detail(team_id: int):
    db = get_db()
    migrate_enemy_teams_to_team_database(db)
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    all_team_scrims = get_scrims_for_team(team["id"], team["name"])
    season_options = get_scrim_season_options(all_team_scrims)
    default_season = get_current_season_from_recent_scrim(all_team_scrims)
    has_unseasoned_scrims = any(not normalize_season_value(scrim.get("season", "")) for scrim in all_team_scrims)
    selected_season = get_selected_season(
        request.args.get("season", ""),
        season_options,
        allow_unspecified=has_unseasoned_scrims,
        default_season=default_season,
    )
    selected_map_type = get_selected_map_type(request.args.get("map_type", "all"))
    team_scrims = filter_scrims_by_season(all_team_scrims, selected_season)
    team_scrims = filter_scrims_by_map_type(team_scrims, selected_map_type)
    predictor_inputs = {
        field_key: (request.args.get(field_key) or "").strip()
        for field_key in PREDICTOR_INPUT_ORDER
    }
    draft_predictor = build_draft_predictor(team_scrims, predictor_inputs)
    team_tournament_rows = build_team_tournament_rows(team)

    staff_roles = {"Coach", "AC", "Analyst"}
    staff_role_options = ["Coach", "AC", "Analyst"]
    all_player_rows = db.execute(
        "SELECT * FROM players WHERE team_id = ? ORDER BY is_sub ASC, name COLLATE NOCASE",
        (team_id,),
    ).fetchall()
    player_rows = [row for row in all_player_rows if (row["role"] or "").strip() not in staff_roles]
    staff_rows = [row for row in all_player_rows if (row["role"] or "").strip() in staff_roles]

    team_analytics = build_scrim_analytics(
        team_scrims,
        roster_player_names=[row["name"] for row in player_rows],
    )

    map_overview = build_team_map_overview(
        team_scrims,
        map_modes=MAP_MODES,
        map_types=MAP_TYPES,
        normalize_map_type_value=normalize_map_type_value,
        get_map_outcome_for_slot=get_map_outcome_for_slot,
        get_map_image_url=get_map_image_url,
    )
    team_map_cards = map_overview["team_map_cards"]
    team_map_mode_rows = map_overview["team_map_mode_rows"]
    best_mode = map_overview["best_mode"]
    worst_mode = map_overview["worst_mode"]
    map_type_visual_rows = map_overview["map_type_visual_rows"]
    opponent_visual_rows = map_overview["opponent_visual_rows"]
    recent_map_visual_rows = map_overview["recent_map_visual_rows"]
    activity_heatmap = map_overview["activity_heatmap"]

    players = []
    for row in player_rows:
        stats = compute_player_stats(row["name"], team_scrims)
        player_breakdown = build_player_hero_map_breakdown(
            row["name"],
            team_scrims,
            team_slots=TEAM_SLOTS,
            canonical_draft_hero=_canonical_draft_hero,
            get_map_outcome_for_slot=get_map_outcome_for_slot,
            map_modes=MAP_MODES,
            get_map_image_url=get_map_image_url,
        )
        primary_hero = player_breakdown["hero_rows"][0]["hero"] if player_breakdown["hero_rows"] else ""
        players.append({
            "id": row["id"],
            "name": row["name"],
            "role": row["role"],
            "is_sub": bool(row["is_sub"]) if "is_sub" in row.keys() else False,
            "main_hero": row["main_hero"],
            "top_hero": primary_hero,
            "notes": row["notes"],
            "stats": stats,
            "hero_rows": player_breakdown.get("hero_rows", []),
        })

    roster_name_lookup = {(player["name"] or "").strip().lower() for player in players if (player.get("name") or "").strip()}
    first_action_events = []
    for scrim in team_scrims:
        scrim_date = (scrim.get("scrim_date") or "").strip()
        opponent_name = (scrim.get("enemy_team") or scrim.get("opponent") or "").strip() or "Opponent"
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            for event in map_entry.get("events", []):
                if not isinstance(event, dict):
                    continue
                killer_player = (event.get("first_kill_player") or event.get("killer_player") or "").strip()
                victim_player = (event.get("first_death_player") or event.get("victim_player") or "").strip()
                if not killer_player and not victim_player:
                    continue
                killer_is_roster = killer_player.lower() in roster_name_lookup if killer_player else False
                victim_is_roster = victim_player.lower() in roster_name_lookup if victim_player else False
                if not killer_is_roster and not victim_is_roster:
                    continue
                fight_winner = (event.get("fight_winner") or "").strip()
                if fight_winner == our_team_slot:
                    fight_result = "Won"
                elif fight_winner:
                    fight_result = "Lost"
                else:
                    fight_result = "Not Set"
                first_action_events.append(
                    {
                        "scrim_id": scrim.get("id"),
                        "map_id": map_entry.get("id"),
                        "scrim_date": scrim_date,
                        "opponent_name": opponent_name,
                        "map_name": (map_entry.get("map_name") or "").strip() or "Unknown Map",
                        "fight_number": (event.get("fight_number") or "").strip(),
                        "killer_player": killer_player,
                        "killer_hero": _canonical_draft_hero(event.get("first_kill_hero") or event.get("killer_hero") or ""),
                        "victim_player": victim_player,
                        "victim_hero": _canonical_draft_hero(event.get("first_death_hero") or event.get("victim_hero") or ""),
                        "fight_result": fight_result,
                        "fight_winner_label": (event.get("fight_winner_label") or "").strip(),
                        "first_action_type": "First Kill" if killer_is_roster else "First Death",
                    }
                )
    first_action_events.sort(
        key=lambda row: (
            row["scrim_date"],
            str(row["scrim_id"] or ""),
            str(row["map_id"] or ""),
            int(row["fight_number"]) if str(row["fight_number"]).isdigit() else 0,
        ),
        reverse=True,
    )
    first_action_enemy_options = sorted({row["opponent_name"] for row in first_action_events if row.get("opponent_name")}, key=str.lower)
    selected_first_action_enemy = (request.args.get("first_action_enemy") or "all").strip()
    if selected_first_action_enemy and selected_first_action_enemy != "all":
        first_action_filtered_events = [
            row for row in first_action_events
            if (row.get("opponent_name") or "").strip().lower() == selected_first_action_enemy.lower()
        ]
    else:
        selected_first_action_enemy = "all"
        first_action_filtered_events = list(first_action_events)

    first_action_read_counts = {
        "total_fights": len(first_action_filtered_events),
        "first_kills": 0,
        "first_deaths": 0,
        "fk_wins": 0,
        "fk_losses": 0,
        "fd_wins": 0,
        "fd_losses": 0,
    }
    fk_player_counter: Counter = Counter()
    fd_player_counter: Counter = Counter()
    fk_target_hero_counter: Counter = Counter()
    fd_source_hero_counter: Counter = Counter()
    roster_first_action_rows = {
        player["name"].strip().lower(): {
            "player": player["name"],
            "role": player.get("role") or "",
            "first_kills": 0,
            "first_deaths": 0,
            "fk_wins": 0,
            "fk_losses": 0,
            "fd_wins": 0,
            "fd_losses": 0,
            "fk_target_heroes": Counter(),
            "fd_source_heroes": Counter(),
        }
        for player in players
        if (player.get("name") or "").strip()
    }

    for row in first_action_filtered_events:
        killer_key = (row.get("killer_player") or "").strip().lower()
        victim_key = (row.get("victim_player") or "").strip().lower()
        fight_result = row.get("fight_result")
        if killer_key in roster_first_action_rows:
            first_action_read_counts["first_kills"] += 1
            fk_player_counter[row["killer_player"]] += 1
            player_read = roster_first_action_rows[killer_key]
            player_read["first_kills"] += 1
            if fight_result == "Won":
                first_action_read_counts["fk_wins"] += 1
                player_read["fk_wins"] += 1
            elif fight_result == "Lost":
                first_action_read_counts["fk_losses"] += 1
                player_read["fk_losses"] += 1
            if row.get("victim_hero"):
                fk_target_hero_counter[row["victim_hero"]] += 1
                player_read["fk_target_heroes"][row["victim_hero"]] += 1
        if victim_key in roster_first_action_rows:
            first_action_read_counts["first_deaths"] += 1
            fd_player_counter[row["victim_player"]] += 1
            player_read = roster_first_action_rows[victim_key]
            player_read["first_deaths"] += 1
            if fight_result == "Won":
                first_action_read_counts["fd_wins"] += 1
                player_read["fd_wins"] += 1
            elif fight_result == "Lost":
                first_action_read_counts["fd_losses"] += 1
                player_read["fd_losses"] += 1
            if row.get("killer_hero"):
                fd_source_hero_counter[row["killer_hero"]] += 1
                player_read["fd_source_heroes"][row["killer_hero"]] += 1

    def _top_counter_row(counter: Counter) -> dict:
        if not counter:
            return {"name": "", "count": 0}
        name, count = counter.most_common(1)[0]
        return {"name": name, "count": count}

    first_action_player_rows = []
    for row in roster_first_action_rows.values():
        fk_target = _top_counter_row(row.pop("fk_target_heroes"))
        fd_source = _top_counter_row(row.pop("fd_source_heroes"))
        row["top_fk_target_hero"] = fk_target["name"]
        row["top_fk_target_hero_count"] = fk_target["count"]
        row["top_fd_source_hero"] = fd_source["name"]
        row["top_fd_source_hero_count"] = fd_source["count"]
        if row["first_kills"] or row["first_deaths"]:
            first_action_player_rows.append(row)
    first_action_player_rows.sort(
        key=lambda row: (row["first_kills"] + row["first_deaths"], row["first_kills"], row["player"].lower()),
        reverse=True,
    )

    first_action_read = {
        **first_action_read_counts,
        "target_label": selected_first_action_enemy if selected_first_action_enemy != "all" else "all opponents",
        "top_fk_player": _top_counter_row(fk_player_counter),
        "top_fd_player": _top_counter_row(fd_player_counter),
        "top_fk_target_hero": _top_counter_row(fk_target_hero_counter),
        "top_fd_source_hero": _top_counter_row(fd_source_hero_counter),
    }

    staff_members = [
        {
            "id": row["id"],
            "name": row["name"],
            "role": row["role"],
            "is_sub": bool(row["is_sub"]) if "is_sub" in row.keys() else False,
            "main_hero": row["main_hero"],
            "notes": row["notes"],
        }
        for row in staff_rows
    ]

    team_hero_profile = build_team_hero_profile(
        team_scrims,
        players,
        team_slots=TEAM_SLOTS,
        canonical_draft_hero=_canonical_draft_hero,
        get_map_outcome_for_slot=get_map_outcome_for_slot,
    )
    hero_graph_rows = team_hero_profile.get("top_heroes", [])
    hero_usage_timeline = build_hero_usage_timeline(
        team_scrims,
        [row["hero"] for row in hero_graph_rows[:6]],
        team_slots=TEAM_SLOTS,
        canonical_map_hero_instances=_canonical_map_hero_instances,
    )

    team_ban_impact = build_team_ban_impact(team_scrims)

    atk_def_wr = build_atk_def_wr(team_scrims, attack_defense_maps=ATTACK_DEFENSE_MAPS)
    pivot_wr = build_pivot_wr(team_scrims, attack_defense_maps=ATTACK_DEFENSE_MAPS)
    scrim_log = build_scrim_log_rows(
        team_scrims,
        target_team_id=team["id"],
        target_team_name=team["name"],
        team_slots=TEAM_SLOTS,
        canonical_draft_hero=_canonical_draft_hero,
        hero_match_key=_hero_match_key,
        hero_role=_hero_role,
        opposite_team_slot=opposite_team_slot,
        get_map_outcome_for_slot=get_map_outcome_for_slot,
        infer_result_from_score_text=infer_result_from_score_text,
        split_score_pair=split_score_pair,
    )
    # Enrich team_map_cards with per-map attack/defense averages
    _atk_def_by_map = {row["map_name"]: row for row in atk_def_wr["per_map"]}
    for _card in team_map_cards:
        _stats = _atk_def_by_map.get(_card["map_name"])
        _card["attack_score_avg"] = _stats["atk_avg"] if _stats else None
        _card["defense_score_avg"] = _stats["def_avg"] if _stats else None

    matchup_context = build_team_detail_matchup_context(
        team_scrims,
        players,
        target_team_id=team["id"],
        target_team_name=team["name"],
        team_slots=TEAM_SLOTS,
        canonical_draft_hero=_canonical_draft_hero,
        hero_match_key=_hero_match_key,
        hero_role=_hero_role,
        opposite_team_slot=opposite_team_slot,
        get_map_outcome_for_slot=get_map_outcome_for_slot,
    )
    matchup_summary = matchup_context["matchup_summary"]
    matchup_rows = matchup_context["matchup_rows"]
    matrix_map_columns = matchup_context["matrix_map_columns"]
    matrix_rows = matchup_context["matrix_rows"]

    return render_template(
        "team_detail.html",
        team=team,
        players=players,
        staff_members=staff_members,
        staff_roles=staff_role_options,
        team_tournament_rows=team_tournament_rows,
        player_roles=PLAYER_ROLES,
        team_analytics=team_analytics,
        season_options=season_options,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
        has_unseasoned_scrims=has_unseasoned_scrims,
        selected_season=selected_season,
        selected_map_type=selected_map_type,
        map_type_options=MAP_TYPES,
        hero_graph_rows=hero_graph_rows,
        hero_usage_timeline=hero_usage_timeline,
        team_scrim_count=len(team_scrims),
        team_scrim_total_count=len(all_team_scrims),
        team_map_cards=team_map_cards,
        team_map_mode_rows=team_map_mode_rows,
        best_mode=best_mode,
        worst_mode=worst_mode,
        map_type_visual_rows=map_type_visual_rows,
        opponent_visual_rows=opponent_visual_rows,
        recent_map_visual_rows=recent_map_visual_rows,
        activity_heatmap=activity_heatmap,
        map_modes=MAP_MODES,
        map_images=MAP_IMAGES,
        draft_predictor=draft_predictor,
        matchup_summary=matchup_summary,
        matchup_rows=matchup_rows,
        player_map_matrix_columns=matrix_map_columns,
        player_map_matrix_rows=matrix_rows,
        team_hero_profile=team_hero_profile,
        hero_transformations=HERO_TRANSFORMATIONS,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
        team_ban_impact=team_ban_impact,
        atk_def_wr=atk_def_wr,
        pivot_wr=pivot_wr,
        scrim_log=scrim_log,
        first_action_events=first_action_filtered_events,
        first_action_read=first_action_read,
        first_action_player_rows=first_action_player_rows,
        first_action_enemy_options=first_action_enemy_options,
        selected_first_action_enemy=selected_first_action_enemy,
    )


@app.route("/tournaments/<int:tournament_id>/teams/<int:tournament_team_id>")
def tournament_team_detail(tournament_id: int, tournament_team_id: int):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_team = get_tournament_team_by_id(tournament_record, tournament_team_id)
    if tournament_team is None:
        abort(404)

    # Sync roster from DB (same pattern as tournament_detail) so imported
    # tournament teams use the canonical database team when names match.
    if sync_tournament_team_with_db(tournament_team):
        normalize_tournament_record(tournament_record)
        tournament_team = get_tournament_team_by_id(tournament_record, tournament_team_id) or tournament_team
        save_app_state()

    source_team = None
    source_team_id = tournament_team.get("source_team_id")
    if isinstance(source_team_id, int):
        source_team = get_db().execute("SELECT * FROM teams WHERE id = ?", (source_team_id,)).fetchone()
    if source_team is None:
        source_team = get_db().execute(
            "SELECT * FROM teams WHERE lower(name) = lower(?)",
            ((tournament_team.get("name") or "").strip(),),
        ).fetchone()

    team_scrims = build_tournament_team_scrims(tournament_record, tournament_team)
    team_analytics = build_scrim_analytics(team_scrims)
    hero_graph_rows = [
        {
            "hero": row["hero"],
            "maps": row["maps"],
            "appearances": row["maps"],
            "pick_rate": round((row["maps"] / team_analytics["summary"]["total_maps"]) * 100, 1)
            if team_analytics["summary"]["total_maps"]
            else 0,
            "usage_rate": round((row["maps"] / team_analytics["summary"]["total_maps"]) * 100, 1)
            if team_analytics["summary"]["total_maps"]
            else 0,
            "win_rate": row.get("win_rate", row.get("unmirrored_win_rate", 0)),
            "unmirrored_win_rate": row["unmirrored_win_rate"],
        }
        for row in team_analytics.get("hero_rows", [])
    ]
    hero_usage_timeline = build_hero_usage_timeline(
        team_scrims,
        [row["hero"] for row in hero_graph_rows[:6]],
        team_slots=TEAM_SLOTS,
        canonical_map_hero_instances=_canonical_map_hero_instances,
    )

    map_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})
    mode_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})
    map_timeline_targets: dict[str, int] = {}
    match_rows = []

    for tournament_match in tournament_record.get("matches", []):
        if tournament_match.get("team1_tournament_team_id") == tournament_team_id:
            opponent_name = tournament_match.get("team2_name") or "Opponent"
        elif tournament_match.get("team2_tournament_team_id") == tournament_team_id:
            opponent_name = tournament_match.get("team1_name") or "Opponent"
        else:
            continue

        wins = 0
        losses = 0
        for map_entry in tournament_match.get("maps", []):
            team_slot = get_tournament_team_slot_for_map(map_entry, tournament_team_id)
            if team_slot is None:
                continue
            map_name = (map_entry.get("map_name", "") or "").strip()
            if map_name:
                mode_name = MAP_MODES.get(map_name, "Other")
                map_records[map_name]["maps"] += 1
                mode_records[mode_name]["maps"] += 1
                if map_name not in map_timeline_targets and tournament_match.get("id") is not None:
                    map_timeline_targets[map_name] = tournament_match.get("id")

            result = get_map_outcome_for_slot(map_entry, team_slot)
            if result == "Win":
                wins += 1
                if map_name:
                    map_records[map_name]["wins"] += 1
                    mode_records[mode_name]["wins"] += 1
            elif result == "Loss":
                losses += 1
                if map_name:
                    map_records[map_name]["losses"] += 1
                    mode_records[mode_name]["losses"] += 1

        match_rows.append(
            {
                "id": tournament_match.get("id"),
                "opponent_name": opponent_name,
                "scrim_date": tournament_match.get("scrim_date") or tournament_record.get("scrim_date", ""),
                "maps": len(tournament_match.get("maps", [])),
                "wins": wins,
                "losses": losses,
                "win_rate": round((wins / (wins + losses)) * 100, 1) if (wins + losses) else 0,
            }
        )

    match_rows.sort(key=lambda row: (row.get("scrim_date") or "", row.get("id") or 0), reverse=True)

    team_map_cards = []
    for map_name, stats in map_records.items():
        maps_played = stats["maps"]
        win_rate = round((stats["wins"] / maps_played) * 100, 1) if maps_played else 0
        team_map_cards.append(
            {
                "map_name": map_name,
                "mode": MAP_MODES.get(map_name, "Other"),
                "maps": maps_played,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": win_rate,
                "image": get_map_image_url(map_name),
                "timeline_match_id": map_timeline_targets.get(map_name),
            }
        )
    team_map_cards.sort(key=lambda row: (row["win_rate"], row["maps"]), reverse=True)

    team_map_mode_rows = []
    for mode_name, stats in mode_records.items():
        maps_played = stats["maps"]
        win_rate = round((stats["wins"] / maps_played) * 100, 1) if maps_played else 0
        mode_maps = [card for card in team_map_cards if card["mode"] == mode_name]
        best_map = max(mode_maps, key=lambda row: (row["win_rate"], row["maps"]), default=None)
        worst_map = min(mode_maps, key=lambda row: (row["win_rate"], -row["maps"]), default=None)
        team_map_mode_rows.append(
            {
                "mode": mode_name,
                "maps": maps_played,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": win_rate,
                "best_map": best_map,
                "worst_map": worst_map,
            }
        )
    team_map_mode_rows.sort(key=lambda row: (row["win_rate"], row["maps"]), reverse=True)

    picked_map_rows = build_tournament_team_pick_rows(tournament_record, tournament_team)
    staff_roles = {"Coach", "AC", "Analyst"}
    players = []
    staff_members = []
    if source_team is not None:
        source_player_rows = get_db().execute(
            "SELECT * FROM players WHERE team_id = ? ORDER BY is_sub ASC, name COLLATE NOCASE",
            (source_team["id"],),
        ).fetchall()
        for player_row in source_player_rows:
            if (player_row["role"] or "").strip() in staff_roles:
                staff_members.append(
                    {
                        "id": player_row["id"],
                        "name": player_row["name"],
                        "role": player_row["role"],
                        "is_sub": bool(player_row["is_sub"]) if "is_sub" in player_row.keys() else False,
                        "main_hero": player_row["main_hero"],
                        "notes": player_row["notes"],
                    }
                )
                continue
            stats = compute_player_stats(player_row["name"], team_scrims)
            players.append(
                {
                    "id": player_row["id"],
                    "name": player_row["name"],
                    "role": player_row["role"],
                    "is_sub": bool(player_row["is_sub"]) if "is_sub" in player_row.keys() else False,
                    "main_hero": player_row["main_hero"],
                    "top_hero": stats.get("top_hero"),
                    "notes": player_row["notes"],
                    "stats": stats,
                }
            )
    else:
        players = [
            {
                "id": None,
                "name": player_name,
                "role": "",
                "is_sub": False,
                "main_hero": "",
                "top_hero": "",
                "notes": "",
                "stats": compute_player_stats(player_name, team_scrims),
            }
            for player_name in tournament_team.get("players", [])
        ]

    return render_template(
        "tournament_team_detail.html",
        tournament=tournament_record,
        tournament_team=tournament_team,
        source_team=source_team,
        team_analytics=team_analytics,
        hero_graph_rows=hero_graph_rows,
        hero_usage_timeline=hero_usage_timeline,
        team_map_cards=team_map_cards,
        team_map_mode_rows=team_map_mode_rows,
        best_mode=team_map_mode_rows[0] if team_map_mode_rows else None,
        worst_mode=team_map_mode_rows[-1] if team_map_mode_rows else None,
        picked_map_rows=picked_map_rows,
        match_rows=match_rows,
        players=players,
        staff_members=staff_members,
        hero_transformations=HERO_TRANSFORMATIONS,
        heroes=HEROES,
        hero_roles=HERO_ROLES,
        map_images=MAP_IMAGES,
    )


