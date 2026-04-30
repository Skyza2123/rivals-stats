# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def _collect_map_draft_intel(source_scrims: list[dict], map_name: str) -> dict:
    ban_slots = ("ban1", "ban2", "ban3", "ban4")
    protect_slots = ("protect1", "protect2")

    our_comp_counts = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    enemy_comp_counts = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})

    our_ban_counts = defaultdict(int)
    enemy_ban_counts = defaultdict(int)
    our_protect_counts = defaultdict(int)
    enemy_protect_counts = defaultdict(int)

    our_ban_slot_counts = defaultdict(lambda: defaultdict(int))
    enemy_ban_slot_counts = defaultdict(lambda: defaultdict(int))
    our_protect_slot_counts = defaultdict(lambda: defaultdict(int))
    enemy_protect_slot_counts = defaultdict(lambda: defaultdict(int))

    map_samples = 0
    our_total_bans = 0
    enemy_total_bans = 0
    our_total_protects = 0
    enemy_total_protects = 0

    def _largest_lineup_from_map(map_entry: dict, team_slot: str) -> list[str]:
        largest_lineup: list[str] = []
        for section in map_entry.get("comp", []):
            if not isinstance(section, dict):
                continue
            lineup = section.get(team_slot, [])
            if not isinstance(lineup, list):
                continue
            hero_rows: list[str] = []
            for slot in lineup:
                if not isinstance(slot, dict):
                    continue
                hero_name = _canonical_draft_hero(slot.get("hero", ""))
                if hero_name:
                    hero_rows.append(hero_name)
            if len(hero_rows) > len(largest_lineup):
                largest_lineup = hero_rows
        return largest_lineup

    for source_scrim in source_scrims:
        for map_entry in source_scrim.get("maps", []):
            if (map_entry.get("map_name") or "").strip() != map_name:
                continue

            map_samples += 1
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            enemy_team_slot = opposite_team_slot(our_team_slot)

            result = get_map_outcome_for_slot(map_entry, our_team_slot)

            our_lineup = _largest_lineup_from_map(map_entry, our_team_slot)
            enemy_lineup = _largest_lineup_from_map(map_entry, enemy_team_slot)
            if our_lineup:
                comp_key = tuple(sorted(our_lineup))
                our_comp_counts[comp_key]["count"] += 1
                if result == "Win":
                    our_comp_counts[comp_key]["wins"] += 1
                elif result == "Loss":
                    our_comp_counts[comp_key]["losses"] += 1
            if enemy_lineup:
                enemy_comp_key = tuple(sorted(enemy_lineup))
                enemy_comp_counts[enemy_comp_key]["count"] += 1
                if result == "Loss":
                    enemy_comp_counts[enemy_comp_key]["wins"] += 1
                elif result == "Win":
                    enemy_comp_counts[enemy_comp_key]["losses"] += 1

            draft = map_entry.get("draft", {})
            our_draft = draft.get(our_team_slot, {}) if isinstance(draft, dict) else {}
            enemy_draft = draft.get(enemy_team_slot, {}) if isinstance(draft, dict) else {}
            if not isinstance(our_draft, dict):
                our_draft = {}
            if not isinstance(enemy_draft, dict):
                enemy_draft = {}

            for slot_key in ban_slots:
                hero_name = _canonical_draft_hero(our_draft.get(slot_key, ""))
                if hero_name:
                    our_ban_counts[hero_name] += 1
                    our_ban_slot_counts[hero_name][slot_key] += 1
                    our_total_bans += 1
                enemy_hero_name = _canonical_draft_hero(enemy_draft.get(slot_key, ""))
                if enemy_hero_name:
                    enemy_ban_counts[enemy_hero_name] += 1
                    enemy_ban_slot_counts[enemy_hero_name][slot_key] += 1
                    enemy_total_bans += 1

            for slot_key in protect_slots:
                hero_name = _canonical_draft_hero(our_draft.get(slot_key, ""))
                if hero_name:
                    our_protect_counts[hero_name] += 1
                    our_protect_slot_counts[hero_name][slot_key] += 1
                    our_total_protects += 1
                enemy_hero_name = _canonical_draft_hero(enemy_draft.get(slot_key, ""))
                if enemy_hero_name:
                    enemy_protect_counts[enemy_hero_name] += 1
                    enemy_protect_slot_counts[enemy_hero_name][slot_key] += 1
                    enemy_total_protects += 1

    def _top_slot(slot_counts: dict[str, int]) -> tuple[str, int]:
        if not slot_counts:
            return "", 0
        slot_key, slot_count = max(slot_counts.items(), key=lambda item: (item[1], item[0]))
        return _draft_slot_label(slot_key), slot_count

    our_comp_rows = [
        {
            "heroes": list(comp_key),
            "count": stats["count"],
            "play_rate": round((stats["count"] / map_samples) * 100, 1) if map_samples else 0,
            "win_rate": round((stats["wins"] / stats["count"]) * 100, 1) if stats["count"] else 0,
        }
        for comp_key, stats in sorted(
            our_comp_counts.items(),
            key=lambda item: (item[1]["count"], item[1]["wins"], item[0]),
            reverse=True,
        )[:10]
    ]
    enemy_comp_rows = [
        {
            "heroes": list(comp_key),
            "count": stats["count"],
            "play_rate": round((stats["count"] / map_samples) * 100, 1) if map_samples else 0,
            "win_rate": round((stats["wins"] / stats["count"]) * 100, 1) if stats["count"] else 0,
        }
        for comp_key, stats in sorted(
            enemy_comp_counts.items(),
            key=lambda item: (item[1]["count"], item[1]["wins"], item[0]),
            reverse=True,
        )[:10]
    ]

    our_ban_rows = []
    for hero_name, count in sorted(our_ban_counts.items(), key=lambda item: (item[1], item[0]), reverse=True)[:12]:
        slot_label, slot_count = _top_slot(our_ban_slot_counts[hero_name])
        our_ban_rows.append(
            {
                "hero": hero_name,
                "count": count,
                "share": round((count / our_total_bans) * 100, 1) if our_total_bans else 0,
                "top_slot": slot_label,
                "top_slot_rate": round((slot_count / count) * 100, 1) if count else 0,
            }
        )

    enemy_ban_rows = []
    for hero_name, count in sorted(enemy_ban_counts.items(), key=lambda item: (item[1], item[0]), reverse=True)[:12]:
        slot_label, slot_count = _top_slot(enemy_ban_slot_counts[hero_name])
        enemy_ban_rows.append(
            {
                "hero": hero_name,
                "count": count,
                "share": round((count / enemy_total_bans) * 100, 1) if enemy_total_bans else 0,
                "top_slot": slot_label,
                "top_slot_rate": round((slot_count / count) * 100, 1) if count else 0,
            }
        )

    our_protect_rows = []
    for hero_name, count in sorted(our_protect_counts.items(), key=lambda item: (item[1], item[0]), reverse=True)[:10]:
        slot_label, slot_count = _top_slot(our_protect_slot_counts[hero_name])
        our_protect_rows.append(
            {
                "hero": hero_name,
                "count": count,
                "share": round((count / our_total_protects) * 100, 1) if our_total_protects else 0,
                "top_slot": slot_label,
                "top_slot_rate": round((slot_count / count) * 100, 1) if count else 0,
            }
        )

    enemy_protect_rows = []
    for hero_name, count in sorted(enemy_protect_counts.items(), key=lambda item: (item[1], item[0]), reverse=True)[:10]:
        slot_label, slot_count = _top_slot(enemy_protect_slot_counts[hero_name])
        enemy_protect_rows.append(
            {
                "hero": hero_name,
                "count": count,
                "share": round((count / enemy_total_protects) * 100, 1) if enemy_total_protects else 0,
                "top_slot": slot_label,
                "top_slot_rate": round((slot_count / count) * 100, 1) if count else 0,
            }
        )

    return {
        "our_comp_rows": our_comp_rows,
        "enemy_comp_rows": enemy_comp_rows,
        "our_ban_rows": our_ban_rows,
        "enemy_ban_rows": enemy_ban_rows,
        "our_protect_rows": our_protect_rows,
        "enemy_protect_rows": enemy_protect_rows,
        "map_samples": map_samples,
    }


@app.route("/scrims/<int:scrim_id>/timelines/<path:map_name>")
def scrim_map_timeline(scrim_id: int, map_name: str):
    scrim = get_scrim_or_404(scrim_id)
    participant_one_label, participant_two_label = get_scrim_participant_labels(scrim)
    participant_one, participant_two = get_scrim_participants(scrim)
    db = get_db()

    def _canonical_team_label(participant: dict, fallback: str) -> str:
        team_id_value = participant.get("id")
        if not team_id_value:
            return fallback
        row = db.execute("SELECT name FROM teams WHERE id = ?", (team_id_value,)).fetchone()
        if row is None:
            return fallback
        canonical_name = str(row["name"] or "").strip()
        return canonical_name or fallback

    participant_one_label = _canonical_team_label(participant_one, participant_one_label)
    participant_two_label = _canonical_team_label(participant_two, participant_two_label)

    team_id = participant_one.get("id") or scrim.get("team_id")
    team_name = (participant_one.get("name") or scrim.get("team_name") or scrim.get("team1_name") or "").strip()
    enemy_team_id = participant_two.get("id")
    enemy_team_name = (participant_two.get("name") or scrim.get("team2_name") or scrim.get("enemy_team") or scrim.get("opponent") or "").strip()
    map_timeline_row = None
    map_overview = {
        "maps": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0,
    }
    top_hero_rows: list[dict] = []
    enemy_top_hero_rows: list[dict] = []
    map_draft_intel = {
        "our_comp_rows": [],
        "enemy_comp_rows": [],
        "our_ban_rows": [],
        "enemy_ban_rows": [],
        "our_protect_rows": [],
        "enemy_protect_rows": [],
        "map_samples": 0,
    }
    if team_id and team_name:
        team_row = db.execute("SELECT id, name FROM teams WHERE id = ?", (team_id,)).fetchone()
        if team_row is not None:
            source_scrims = get_team_history_scrims(team_row)
        else:
            source_scrims = get_scrims_for_team(team_id, team_name)

        # Keep this page matchup-specific (Team A vs Team B), not broad all-opponents history.
        if enemy_team_id is not None or enemy_team_name:
            source_scrims = [
                source_scrim
                for source_scrim in source_scrims
                if scrim_involves_team(source_scrim, enemy_team_id, enemy_team_name)
            ]

        map_draft_intel = _collect_map_draft_intel(source_scrims, map_name)
        draft_timeline = build_draft_phase_timeline(source_scrims)
        map_timeline_row = next(
            (row for row in draft_timeline.get("maps", []) if row.get("map_name") == map_name),
            None,
        )

        our_hero_counts = defaultdict(int)
        our_hero_win_counts = defaultdict(int)
        enemy_hero_counts = defaultdict(int)
        enemy_hero_win_counts = defaultdict(int)
        total_our_instances = 0
        total_enemy_instances = 0
        map_count = 0
        win_count = 0
        loss_count = 0
        for source_scrim in source_scrims:
            for map_entry in source_scrim.get("maps", []):
                if (map_entry.get("map_name") or "").strip() != map_name:
                    continue
                map_count += 1
                our_team_slot = map_entry.get("our_team_slot", "team1")
                if our_team_slot not in TEAM_SLOTS:
                    our_team_slot = "team1"
                enemy_team_slot = opposite_team_slot(our_team_slot)

                result = get_map_outcome_for_slot(map_entry, our_team_slot)
                is_win = result == "Win"
                is_enemy_win = result == "Loss"
                if is_win:
                    win_count += 1
                elif result == "Loss":
                    loss_count += 1

                heroes_in_map = _canonical_map_hero_instances(map_entry, our_team_slot)
                enemy_heroes_in_map = _canonical_map_hero_instances(map_entry, enemy_team_slot)
                total_our_instances += len(heroes_in_map)
                total_enemy_instances += len(enemy_heroes_in_map)

                for hero_name in heroes_in_map:
                    our_hero_counts[hero_name] += 1
                    if is_win:
                        our_hero_win_counts[hero_name] += 1
                for hero_name in enemy_heroes_in_map:
                    enemy_hero_counts[hero_name] += 1
                    if is_enemy_win:
                        enemy_hero_win_counts[hero_name] += 1

        map_overview = {
            "maps": map_count,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": round((win_count / map_count) * 100, 1) if map_count else 0,
        }
        top_hero_rows = [
            {
                "hero": hero_name,
                "appearances": hero_maps,
                "play_rate": round((hero_maps / total_our_instances) * 100, 1) if total_our_instances else 0,
                "win_rate": round((our_hero_win_counts.get(hero_name, 0) / hero_maps) * 100, 1) if hero_maps else 0,
            }
            for hero_name, hero_maps in sorted(our_hero_counts.items(), key=lambda item: (-item[1], item[0].lower()))[:12]
        ]
        enemy_top_hero_rows = [
            {
                "hero": hero_name,
                "appearances": hero_maps,
                "play_rate": round((hero_maps / total_enemy_instances) * 100, 1) if total_enemy_instances else 0,
                "win_rate": round((enemy_hero_win_counts.get(hero_name, 0) / hero_maps) * 100, 1) if hero_maps else 0,
            }
            for hero_name, hero_maps in sorted(enemy_hero_counts.items(), key=lambda item: (-item[1], item[0].lower()))[:12]
        ]

    return render_template(
        "map_timeline_detail.html",
        map_name=map_name,
        map_timeline_row=map_timeline_row,
        map_overview=map_overview,
        top_hero_rows=top_hero_rows,
        enemy_top_hero_rows=enemy_top_hero_rows,
        our_comp_rows=map_draft_intel["our_comp_rows"],
        enemy_comp_rows=map_draft_intel["enemy_comp_rows"],
        our_ban_rows=map_draft_intel["our_ban_rows"],
        enemy_ban_rows=map_draft_intel["enemy_ban_rows"],
        our_protect_rows=map_draft_intel["our_protect_rows"],
        enemy_protect_rows=map_draft_intel["enemy_protect_rows"],
        participant_one_label=participant_one_label,
        participant_two_label=participant_two_label,
        is_tournament=False,
        back_to_maps_url=(url_for("team_detail", team_id=scrim.get("team_id")) + "#maps") if scrim.get("team_id") else url_for("teams"),
    )


@app.route("/teams/<int:team_id>/timelines/<path:map_name>")
def team_map_timeline(team_id: int, map_name: str):
    db = get_db()
    team_row = db.execute("SELECT id, name FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team_row is None:
        abort(404)

    source_scrims = get_team_history_scrims(team_row)
    participant_one_label = (team_row["name"] or "").strip() or "Your Team"
    participant_two_label = "All Opponents"

    map_timeline_row = None
    map_overview = {
        "maps": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0,
    }
    top_hero_rows: list[dict] = []
    enemy_top_hero_rows: list[dict] = []
    map_draft_intel = _collect_map_draft_intel(source_scrims, map_name)

    draft_timeline = build_draft_phase_timeline(source_scrims)
    map_timeline_row = next(
        (row for row in draft_timeline.get("maps", []) if row.get("map_name") == map_name),
        None,
    )

    our_hero_counts = defaultdict(int)
    our_hero_win_counts = defaultdict(int)
    enemy_hero_counts = defaultdict(int)
    enemy_hero_win_counts = defaultdict(int)
    total_our_instances = 0
    total_enemy_instances = 0
    map_count = 0
    win_count = 0
    loss_count = 0

    for source_scrim in source_scrims:
        for map_entry in source_scrim.get("maps", []):
            if (map_entry.get("map_name") or "").strip() != map_name:
                continue
            map_count += 1
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            enemy_team_slot = opposite_team_slot(our_team_slot)

            result = get_map_outcome_for_slot(map_entry, our_team_slot)
            is_win = result == "Win"
            is_enemy_win = result == "Loss"
            if is_win:
                win_count += 1
            elif result == "Loss":
                loss_count += 1

            heroes_in_map = _canonical_map_hero_instances(map_entry, our_team_slot)
            enemy_heroes_in_map = _canonical_map_hero_instances(map_entry, enemy_team_slot)
            total_our_instances += len(heroes_in_map)
            total_enemy_instances += len(enemy_heroes_in_map)

            for hero_name in heroes_in_map:
                our_hero_counts[hero_name] += 1
                if is_win:
                    our_hero_win_counts[hero_name] += 1
            for hero_name in enemy_heroes_in_map:
                enemy_hero_counts[hero_name] += 1
                if is_enemy_win:
                    enemy_hero_win_counts[hero_name] += 1

    map_overview = {
        "maps": map_count,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": round((win_count / map_count) * 100, 1) if map_count else 0,
    }
    top_hero_rows = [
        {
            "hero": hero_name,
            "appearances": hero_maps,
            "play_rate": round((hero_maps / total_our_instances) * 100, 1) if total_our_instances else 0,
            "win_rate": round((our_hero_win_counts.get(hero_name, 0) / hero_maps) * 100, 1) if hero_maps else 0,
        }
        for hero_name, hero_maps in sorted(our_hero_counts.items(), key=lambda item: (-item[1], item[0].lower()))[:12]
    ]
    enemy_top_hero_rows = [
        {
            "hero": hero_name,
            "appearances": hero_maps,
            "play_rate": round((hero_maps / total_enemy_instances) * 100, 1) if total_enemy_instances else 0,
            "win_rate": round((enemy_hero_win_counts.get(hero_name, 0) / hero_maps) * 100, 1) if hero_maps else 0,
        }
        for hero_name, hero_maps in sorted(enemy_hero_counts.items(), key=lambda item: (-item[1], item[0].lower()))[:12]
    ]

    return render_template(
        "map_timeline_detail.html",
        map_name=map_name,
        map_timeline_row=map_timeline_row,
        map_overview=map_overview,
        top_hero_rows=top_hero_rows,
        enemy_top_hero_rows=enemy_top_hero_rows,
        our_comp_rows=map_draft_intel["our_comp_rows"],
        enemy_comp_rows=map_draft_intel["enemy_comp_rows"],
        our_ban_rows=map_draft_intel["our_ban_rows"],
        enemy_ban_rows=map_draft_intel["enemy_ban_rows"],
        our_protect_rows=map_draft_intel["our_protect_rows"],
        enemy_protect_rows=map_draft_intel["enemy_protect_rows"],
        participant_one_label=participant_one_label,
        participant_two_label=participant_two_label,
        is_tournament=False,
        back_to_maps_url=url_for("team_detail", team_id=team_id) + "#maps",
    )


@app.route("/tournaments/<int:tournament_id>/matches/<int:match_id>/timelines/<path:map_name>")
def tournament_match_map_timeline(tournament_id: int, match_id: int, map_name: str):
    tournament_record = get_tournament_or_404(tournament_id)
    tournament_match = get_tournament_match_or_404(tournament_record, match_id)

    perspective = tournament_match.get("our_team_slot", "team1") if tournament_match.get("our_team_slot", "team1") in TEAM_SLOTS else "team1"
    source_scrims = build_tournament_match_scrims(tournament_record, perspective=perspective)
    map_draft_intel = _collect_map_draft_intel(source_scrims, map_name)
    draft_timeline = build_draft_phase_timeline(source_scrims)
    map_timeline_row = next(
        (row for row in draft_timeline.get("maps", []) if row.get("map_name") == map_name),
        None,
    )

    our_hero_counts = defaultdict(int)
    our_hero_win_counts = defaultdict(int)
    enemy_hero_counts = defaultdict(int)
    enemy_hero_win_counts = defaultdict(int)
    map_count = 0
    win_count = 0
    loss_count = 0
    for source_scrim in source_scrims:
        for map_entry in source_scrim.get("maps", []):
            if (map_entry.get("map_name") or "").strip() != map_name:
                continue
            map_count += 1
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            enemy_team_slot = opposite_team_slot(our_team_slot)

            result = get_map_outcome_for_slot(map_entry, our_team_slot)
            is_win = result == "Win"
            is_enemy_win = result == "Loss"
            if is_win:
                win_count += 1
            elif result == "Loss":
                loss_count += 1

            heroes_in_map = set()
            enemy_heroes_in_map = set()
            for section in map_entry.get("comp", []):
                for slot in section.get(our_team_slot, []):
                    hero_name = _resolve_hero_transform_key((slot.get("hero") or "").strip()) or (slot.get("hero") or "").strip()
                    if hero_name:
                        heroes_in_map.add(hero_name)
                for slot in section.get(enemy_team_slot, []):
                    hero_name = _resolve_hero_transform_key((slot.get("hero") or "").strip()) or (slot.get("hero") or "").strip()
                    if hero_name:
                        enemy_heroes_in_map.add(hero_name)

            for hero_name in heroes_in_map:
                our_hero_counts[hero_name] += 1
                if is_win:
                    our_hero_win_counts[hero_name] += 1
            for hero_name in enemy_heroes_in_map:
                enemy_hero_counts[hero_name] += 1
                if is_enemy_win:
                    enemy_hero_win_counts[hero_name] += 1

    map_overview = {
        "maps": map_count,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": round((win_count / map_count) * 100, 1) if map_count else 0,
    }
    total_our_instances = sum(our_hero_counts.values())
    total_enemy_instances = sum(enemy_hero_counts.values())
    top_hero_rows = [
        {
            "hero": hero_name,
            "appearances": hero_maps,
            "play_rate": round((hero_maps / total_our_instances) * 100, 1) if total_our_instances else 0,
            "win_rate": round((our_hero_win_counts.get(hero_name, 0) / hero_maps) * 100, 1) if hero_maps else 0,
        }
        for hero_name, hero_maps in sorted(our_hero_counts.items(), key=lambda item: (-item[1], item[0].lower()))[:12]
    ]
    enemy_top_hero_rows = [
        {
            "hero": hero_name,
            "appearances": hero_maps,
            "play_rate": round((hero_maps / total_enemy_instances) * 100, 1) if total_enemy_instances else 0,
            "win_rate": round((enemy_hero_win_counts.get(hero_name, 0) / hero_maps) * 100, 1) if hero_maps else 0,
        }
        for hero_name, hero_maps in sorted(enemy_hero_counts.items(), key=lambda item: (-item[1], item[0].lower()))[:12]
    ]

    team1_label = (get_tournament_team_by_id(tournament_record, tournament_match.get("team1_tournament_team_id")) or {}).get("name") or tournament_match.get("team1_name") or "Team 1"
    team2_label = (get_tournament_team_by_id(tournament_record, tournament_match.get("team2_tournament_team_id")) or {}).get("name") or tournament_match.get("team2_name") or "Team 2"

    return render_template(
        "map_timeline_detail.html",
        map_name=map_name,
        map_timeline_row=map_timeline_row,
        map_overview=map_overview,
        top_hero_rows=top_hero_rows,
        enemy_top_hero_rows=enemy_top_hero_rows,
        our_comp_rows=map_draft_intel["our_comp_rows"],
        enemy_comp_rows=map_draft_intel["enemy_comp_rows"],
        our_ban_rows=map_draft_intel["our_ban_rows"],
        enemy_ban_rows=map_draft_intel["enemy_ban_rows"],
        our_protect_rows=map_draft_intel["our_protect_rows"],
        enemy_protect_rows=map_draft_intel["enemy_protect_rows"],
        participant_one_label=team1_label,
        participant_two_label=team2_label,
        is_tournament=True,
        back_to_maps_url=(
            url_for("tournament_team_detail", tournament_id=tournament_id, tournament_team_id=parse_team_id(request.args.get("tournament_team_id", ""))) + "#maps"
            if parse_team_id(request.args.get("tournament_team_id", "")) is not None
            else url_for("tournament_detail", tournament_id=tournament_id)
        ),
    )


