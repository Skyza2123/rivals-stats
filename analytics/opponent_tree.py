# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def build_opponent_tree_model(team_scrims: list[dict], hero_pool_scrims: list[dict] | None = None) -> dict:
    if hero_pool_scrims is None:
        hero_pool_scrims = team_scrims

    if not team_scrims and not hero_pool_scrims:
        return {
            "status": "empty",
            "training_maps": 0,
            "comfort_core_rows": [],
            "hero_pool_rows": [],
            "ban_line_rows": [],
            "comp_rows": [],
            "comp_path_rows": [],
            "volatile_hero_rows": [],
            "pivot_rows": [],
            "mode_hero_rows": [],
            "mode_comp_rows": [],
            "equivalent_path_rows": [],
            "map_state_rows": [],
        }

    def map_type_weight(raw_map_type: str) -> float:
        normalized = normalize_map_type_value(raw_map_type)
        if normalized == "PTW":
            return 1.75
        if normalized == "Test":
            return 0.55
        return 1.0

    def state_label(required_modes: set[str], *, is_first_map: bool) -> str:
        if is_first_map:
            return "Opening Control"
        if not required_modes:
            return "Cycle Reset"
        ordered = [mode for mode in ("Control", "Escort", "Hybrid") if mode in required_modes]
        if len(ordered) == 1:
            return f"Need {ordered[0]}"
        if len(ordered) == 2:
            return f"Need {' or '.join(ordered)}"
        return "Open Cycle"

    def legal_modes_for_next_map(index: int, cycle_modes_played: set[str]) -> set[str]:
        if index == 0:
            return {"Control"}
        if len(cycle_modes_played) == 3:
            cycle_modes_played = set()
        return {"Control", "Escort", "Hybrid"} - cycle_modes_played

    def split_line(line_values: tuple[str, ...] | list[str]) -> dict[str, list[str]]:
        values = list(line_values)
        return {
            "bans": [values[i] for i in (0, 2, 3, 5) if i < len(values) and values[i]],
            "protects": [values[i] for i in (1, 4) if i < len(values) and values[i]],
        }

    def line_distance(a: tuple[str, ...] | list[str], b: tuple[str, ...] | list[str]) -> int:
        a_values = list(a)
        b_values = list(b)
        return sum(1 for idx in range(min(len(a_values), len(b_values))) if a_values[idx] != b_values[idx])

    def weighted_linear_delta_pct(points: list[tuple[float, float, float]]) -> float:
        if len(points) < MACHINE_TREND_MIN_POINTS:
            return 0.0
        valid_points = [(x, y, w) for x, y, w in points if w > 0]
        if len(valid_points) < MACHINE_TREND_MIN_POINTS:
            return 0.0

        min_x = min(x for x, _y, _w in valid_points)
        max_x = max(x for x, _y, _w in valid_points)
        x_range = max_x - min_x
        if x_range <= 0:
            return 0.0

        sum_w = sum(w for _x, _y, w in valid_points)
        if sum_w <= 0:
            return 0.0

        mean_x = sum(x * w for x, _y, w in valid_points) / sum_w
        mean_y = sum(y * w for _x, y, w in valid_points) / sum_w
        cov_xy = sum(w * (x - mean_x) * (y - mean_y) for x, y, w in valid_points)
        var_x = sum(w * (x - mean_x) ** 2 for x, _y, w in valid_points)
        if var_x <= 1e-9:
            return 0.0

        slope = cov_xy / var_x
        return slope * x_range * 100.0

    analytics = build_scrim_analytics(team_scrims)
    total_maps = analytics.get("summary", {}).get("total_maps", 0)
    weighted_total_maps = 0.0
    weighted_total_wins = 0.0
    recency_decay_lambda = math.log(2.0) / RECENCY_HALFLIFE_DAYS
    dated_scrim_dates = [
        parsed
        for parsed in (
            _parse_scrim_date(scrim.get("scrim_date", ""))
            or _infer_date_from_season(scrim.get("season", ""))
            for scrim in team_scrims
        )
        if parsed is not None
    ]
    newest_scrim_date = max(dated_scrim_dates) if dated_scrim_dates else None
    earliest_scrim_date = min(dated_scrim_dates) if dated_scrim_dates else None

    hero_weighted_apps = defaultdict(float)
    hero_weighted_wins = defaultdict(float)
    hero_weighted_players = defaultdict(set)
    hero_player_weighted_apps: dict[str, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
    hero_player_weighted_wins: dict[str, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
    line_counts: defaultdict[tuple[str, ...], float] = defaultdict(float)
    line_wins: defaultdict[tuple[str, ...], float] = defaultdict(float)
    comp_counts: defaultdict[tuple[str, ...], float] = defaultdict(float)
    comp_wins: defaultdict[tuple[str, ...], float] = defaultdict(float)
    line_to_comp_counts: dict[tuple[str, ...], defaultdict[tuple[str, ...], float]] = defaultdict(lambda: defaultdict(float))
    comp_to_line_counts: dict[tuple[str, ...], defaultdict[tuple[str, ...], float]] = defaultdict(lambda: defaultdict(float))
    hero_presence_maps: defaultdict[str, float] = defaultdict(float)
    hero_presence_wins: defaultdict[str, float] = defaultdict(float)
    mode_hero_counts: dict[str, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
    mode_comp_counts: dict[str, defaultdict[tuple[str, ...], float]] = defaultdict(lambda: defaultdict(float))
    mode_totals: defaultdict[str, float] = defaultdict(float)
    mode_comp_totals: defaultdict[str, float] = defaultdict(float)
    map_state_counts: dict[str, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
    map_state_totals: defaultdict[str, float] = defaultdict(float)
    hero_trend_points: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    comp_trend_points: dict[tuple[str, ...], list[tuple[float, float, float]]] = defaultdict(list)
    map_time_index = 0
    undated_offset = (newest_scrim_date - earliest_scrim_date).days + 1 if newest_scrim_date and earliest_scrim_date else 0

    def scrim_sort_key(item: tuple[int, dict]) -> tuple[bool, date, int]:
        idx, scrim = item
        scrim_date = (
            _parse_scrim_date(scrim.get("scrim_date", ""))
            or _infer_date_from_season(scrim.get("season", ""))
        )
        return (scrim_date is None, scrim_date or date.min, idx)

    for _scrim_idx, scrim in sorted(enumerate(team_scrims), key=scrim_sort_key):
        scrim_date = (
            _parse_scrim_date(scrim.get("scrim_date", ""))
            or _infer_date_from_season(scrim.get("season", ""))
        )
        recency_weight = 1.0
        if newest_scrim_date is not None and scrim_date is not None:
            age_days = max(0, (newest_scrim_date - scrim_date).days)
            recency_weight = math.exp(-recency_decay_lambda * age_days)

        used_maps_in_series: set[str] = set()
        cycle_modes_played: set[str] = set()
        ordered_maps = [map_entry for map_entry in scrim.get("maps", []) if isinstance(map_entry, dict)]

        for index, map_entry in enumerate(ordered_maps):
            map_time_index += 1
            if scrim_date is not None and earliest_scrim_date is not None:
                time_x = float((scrim_date - earliest_scrim_date).days)
            else:
                time_x = float(undated_offset + map_time_index)
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            outcome = get_map_outcome_for_slot(map_entry, our_team_slot)
            decided_outcome = 1.0 if outcome == "Win" else 0.0 if outcome == "Loss" else None
            map_weight = map_type_weight(map_entry.get("map_type", "")) * recency_weight
            weighted_total_maps += map_weight
            if outcome == "Win":
                weighted_total_wins += map_weight

            map_seen_heroes: set[str] = set()
            map_seen_hero_players: set[tuple[str, str]] = set()
            draft = map_entry.get("draft", {})
            our_draft = draft.get(our_team_slot, {}) if isinstance(draft, dict) else {}
            if not isinstance(our_draft, dict):
                our_draft = {}

            line_values: list[str] = []
            line_is_complete = True
            for slot_key in DRAFT_SLOT_ORDER:
                hero_name = _canonical_draft_hero(our_draft.get(slot_key, ""))
                if hero_name:
                    line_values.append(hero_name)
                else:
                    line_values.append("")
                    line_is_complete = False

            if line_is_complete:
                line_key = tuple(line_values)
                line_counts[line_key] += map_weight
                if outcome == "Win":
                    line_wins[line_key] += map_weight

            richest_comp = _predictor_richest_comp(map_entry, our_team_slot)
            if richest_comp:
                comp_counts[richest_comp] += map_weight
                if outcome == "Win":
                    comp_wins[richest_comp] += map_weight
                if decided_outcome is not None:
                    comp_trend_points[richest_comp].append((time_x, decided_outcome, map_weight))
                if line_is_complete:
                    line_key = tuple(line_values)
                    line_to_comp_counts[line_key][richest_comp] += map_weight
                    comp_to_line_counts[richest_comp][line_key] += map_weight
                for hero_name in set(richest_comp):
                    hero_presence_maps[hero_name] += map_weight
                    if outcome == "Win":
                        hero_presence_wins[hero_name] += map_weight

            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                lineup = section.get(our_team_slot, [])
                if not isinstance(lineup, list):
                    continue
                for slot in lineup:
                    if not isinstance(slot, dict):
                        continue
                    hero_name = _canonical_draft_hero(slot.get("hero", ""))
                    player_name = (slot.get("player", "") or "").strip()
                    if not hero_name:
                        continue
                    if hero_name not in map_seen_heroes:
                        hero_weighted_apps[hero_name] += map_weight
                        if outcome == "Win":
                            hero_weighted_wins[hero_name] += map_weight
                        map_seen_heroes.add(hero_name)
                    if player_name:
                        hero_weighted_players[hero_name].add(player_name)
                        hero_player_key = (hero_name, player_name)
                        if hero_player_key not in map_seen_hero_players:
                            hero_player_weighted_apps[hero_name][player_name] += map_weight
                            if outcome == "Win":
                                hero_player_weighted_wins[hero_name][player_name] += map_weight
                            map_seen_hero_players.add(hero_player_key)

            if decided_outcome is not None:
                for hero_name in map_seen_heroes:
                    hero_trend_points[hero_name].append((time_x, decided_outcome, map_weight))

            map_name = (map_entry.get("map_name", "") or "").strip()
            mode_name = MAP_MODES.get(map_name, "Other")
            if mode_name in {"Control", "Escort", "Hybrid"}:
                mode_totals[mode_name] += map_weight
                if richest_comp:
                    mode_comp_counts[mode_name][richest_comp] += map_weight
                    mode_comp_totals[mode_name] += map_weight
                    for hero_name in set(richest_comp):
                        mode_hero_counts[mode_name][hero_name] += map_weight
            legal_modes = legal_modes_for_next_map(index, cycle_modes_played)
            legal_maps = [
                candidate
                for candidate, candidate_mode in MAP_MODES.items()
                if candidate_mode in legal_modes and candidate not in used_maps_in_series
            ]
            label = state_label(legal_modes, is_first_map=(index == 0))
            if map_name and map_name in legal_maps:
                map_state_counts[label][map_name] += map_weight
                map_state_totals[label] += map_weight

            used_maps_in_series.add(map_name)
            if mode_name in {"Control", "Escort", "Hybrid"}:
                if len(cycle_modes_played) == 3:
                    cycle_modes_played = set()
                cycle_modes_played.add(mode_name)

    hero_pool_dated_scrim_dates = [
        parsed
        for parsed in (
            _parse_scrim_date(scrim.get("scrim_date", ""))
            or _infer_date_from_season(scrim.get("season", ""))
            for scrim in hero_pool_scrims
        )
        if parsed is not None
    ]
    newest_hero_pool_date = max(hero_pool_dated_scrim_dates) if hero_pool_dated_scrim_dates else None
    earliest_hero_pool_date = min(hero_pool_dated_scrim_dates) if hero_pool_dated_scrim_dates else None
    hero_pool_undated_offset = (
        (newest_hero_pool_date - earliest_hero_pool_date).days + 1
        if newest_hero_pool_date and earliest_hero_pool_date else 0
    )

    hero_weighted_apps = defaultdict(float)
    hero_weighted_wins = defaultdict(float)
    hero_raw_apps = defaultdict(int)
    hero_weighted_players = defaultdict(set)
    hero_player_weighted_apps = defaultdict(lambda: defaultdict(float))
    hero_player_weighted_wins = defaultdict(lambda: defaultdict(float))
    hero_player_raw_apps = defaultdict(lambda: defaultdict(int))
    hero_trend_points = defaultdict(list)
    hero_pool_weighted_total_maps = 0.0
    hero_pool_weighted_total_wins = 0.0
    hero_pool_map_time_index = 0

    for _scrim_idx, scrim in sorted(enumerate(hero_pool_scrims), key=scrim_sort_key):
        scrim_date = (
            _parse_scrim_date(scrim.get("scrim_date", ""))
            or _infer_date_from_season(scrim.get("season", ""))
        )
        recency_weight = 1.0
        if newest_hero_pool_date is not None and scrim_date is not None:
            age_days = max(0, (newest_hero_pool_date - scrim_date).days)
            recency_weight = math.exp(-recency_decay_lambda * age_days)

        ordered_maps = [map_entry for map_entry in scrim.get("maps", []) if isinstance(map_entry, dict)]
        for map_entry in ordered_maps:
            hero_pool_map_time_index += 1
            if scrim_date is not None and earliest_hero_pool_date is not None:
                time_x = float((scrim_date - earliest_hero_pool_date).days)
            else:
                time_x = float(hero_pool_undated_offset + hero_pool_map_time_index)
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            outcome = get_map_outcome_for_slot(map_entry, our_team_slot)
            decided_outcome = 1.0 if outcome == "Win" else 0.0 if outcome == "Loss" else None
            map_weight = map_type_weight(map_entry.get("map_type", "")) * recency_weight
            hero_pool_weighted_total_maps += map_weight
            if outcome == "Win":
                hero_pool_weighted_total_wins += map_weight

            map_seen_heroes: set[str] = set()
            map_seen_hero_players: set[tuple[str, str]] = set()
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                lineup = section.get(our_team_slot, [])
                if not isinstance(lineup, list):
                    continue
                for slot in lineup:
                    if not isinstance(slot, dict):
                        continue
                    hero_name = _canonical_draft_hero(slot.get("hero", ""))
                    player_name = (slot.get("player", "") or "").strip()
                    if not hero_name:
                        continue
                    if hero_name not in map_seen_heroes:
                        hero_weighted_apps[hero_name] += map_weight
                        hero_raw_apps[hero_name] += 1
                        if outcome == "Win":
                            hero_weighted_wins[hero_name] += map_weight
                        map_seen_heroes.add(hero_name)
                    if player_name:
                        hero_weighted_players[hero_name].add(player_name)
                        hero_player_key = (hero_name, player_name)
                        if hero_player_key not in map_seen_hero_players:
                            hero_player_weighted_apps[hero_name][player_name] += map_weight
                            hero_player_raw_apps[hero_name][player_name] += 1
                            if outcome == "Win":
                                hero_player_weighted_wins[hero_name][player_name] += map_weight
                            map_seen_hero_players.add(hero_player_key)

            if decided_outcome is not None:
                for hero_name in map_seen_heroes:
                    hero_trend_points[hero_name].append((time_x, decided_outcome, map_weight))

    hero_trend_deltas = {
        hero_name: weighted_linear_delta_pct(points)
        for hero_name, points in hero_trend_points.items()
    }
    comp_trend_deltas = {
        comp_key: weighted_linear_delta_pct(points)
        for comp_key, points in comp_trend_points.items()
    }

    overall_wr = (hero_pool_weighted_total_wins / hero_pool_weighted_total_maps) if hero_pool_weighted_total_maps else 0.0
    hero_lookup: dict[str, dict] = {}
    for hero_name, appearances in hero_weighted_apps.items():
        # comfort = play rate: fraction of maps where this hero was played (0.0-1.0).
        # Using hero_pool_weighted_total_maps (not sum of all hero picks) so that a hero
        # played every map gets comfort=1.0 rather than ~1/6.
        comfort = (appearances / hero_pool_weighted_total_maps) if hero_pool_weighted_total_maps else 0.0
        raw_wr = (hero_weighted_wins[hero_name] / appearances) if appearances else 0.0
        confidence = min(1.0, appearances / 20.0) if appearances else 0.0
        adjusted_wr = (confidence * raw_wr) + ((1.0 - confidence) * overall_wr)
        base_profile_score = ((comfort * 0.6) + (adjusted_wr * 0.4)) * 100
        trend_delta = hero_trend_deltas.get(hero_name, 0.0)
        trend_confidence = min(1.0, appearances / 10.0) if appearances else 0.0
        trend_bonus = max(
            -MACHINE_HERO_TREND_CAP,
            min(MACHINE_HERO_TREND_CAP, trend_delta * MACHINE_HERO_TREND_BLEND * trend_confidence),
        )
        profile_score = round(max(0.0, min(100.0, base_profile_score + trend_bonus)), 1)
        player_rows = []
        for player_name, player_apps in sorted(
            hero_player_weighted_apps.get(hero_name, {}).items(),
            key=lambda item: (item[1], item[0].lower()),
            reverse=True,
        ):
            player_wr = (
                hero_player_weighted_wins[hero_name][player_name] / player_apps
                if player_apps else 0.0
            )
            player_rows.append(
                {
                    "player": player_name,
                    "maps": int(hero_player_raw_apps.get(hero_name, {}).get(player_name, 0) or 0),
                    "appearances": round(player_apps, 2),
                    "win_rate": round(player_wr * 100, 1),
                }
            )
        hero_lookup[hero_name] = {
            "hero": hero_name,
            "maps": int(hero_raw_apps.get(hero_name, 0) or 0),
            "appearances": round(appearances, 2),
            "comfort_rate": round(comfort * 100, 1),
            "raw_win_rate": round(raw_wr * 100, 1),
            "adjusted_win_rate": round(adjusted_wr * 100, 1),
            "confidence": round(confidence * 100, 1),
            "trend_delta_pp": round(trend_delta, 1),
            "trend_bonus": round(trend_bonus, 1),
            "base_profile_score": round(base_profile_score, 1),
            "player_count": len(hero_weighted_players.get(hero_name, set())),
            "primary_player": player_rows[0]["player"] if player_rows else "",
            "top_players": player_rows[:3],
            "profile_score": profile_score,
        }

    comfort_core_rows = sorted(
        [
            row
            for row in hero_lookup.values()
            if float(row.get("comfort_rate", 0) or 0) >= COMFORT_CORE_MIN_RATE
        ],
        key=lambda row: (row["comfort_rate"], row["appearances"], row["hero"].lower()),
        reverse=True,
    )[:12]

    hero_pool_rows = sorted(
        hero_lookup.values(),
        key=lambda row: (row["profile_score"], row["appearances"], row["hero"].lower()),
        reverse=True,
    )[:12]

    player_hero_apps: dict[str, float] = defaultdict(float)
    player_hero_wins: dict[str, float] = defaultdict(float)
    player_hero_breakdown: dict[str, list[dict]] = {}
    for hero_name, player_apps_map in hero_player_weighted_apps.items():
        for player_name, player_apps in player_apps_map.items():
            player_hero_apps[player_name] += float(player_apps or 0.0)
            player_hero_wins[player_name] += float(hero_player_weighted_wins.get(hero_name, {}).get(player_name, 0.0) or 0.0)

    for player_name, total_apps in player_hero_apps.items():
        hero_rows = []
        for hero_name, player_apps in hero_player_weighted_apps.items():
            apps = float(player_apps.get(player_name, 0.0) or 0.0)
            if apps <= 0:
                continue
            wins = float(hero_player_weighted_wins.get(hero_name, {}).get(player_name, 0.0) or 0.0)
            raw_maps = int(hero_player_raw_apps.get(hero_name, {}).get(player_name, 0) or 0)
            hero_rows.append(
                {
                    "hero": hero_name,
                    "maps": raw_maps,
                    "appearances": round(apps, 2),
                    "usage_rate": round((apps / total_apps) * 100, 1) if total_apps else 0.0,
                    "win_rate": round((wins / apps) * 100, 1) if apps else 0.0,
                }
            )
        hero_rows.sort(key=lambda row: (row["maps"], row["appearances"], row["usage_rate"], row["hero"].lower()), reverse=True)
        player_hero_breakdown[player_name] = hero_rows

    player_hero_rows = [
        {
            "player": player_name,
            "maps": int(sum(row.get("maps", 0) for row in player_hero_breakdown.get(player_name, []))),
            "appearances": round(total_apps, 2),
            "overall_win_rate": round((player_hero_wins.get(player_name, 0.0) / total_apps) * 100, 1) if total_apps else 0.0,
            "top_heroes": player_hero_breakdown.get(player_name, []),
        }
        for player_name, total_apps in sorted(
            player_hero_apps.items(),
            key=lambda item: (item[1], item[0].lower()),
            reverse=True,
        )
    ]

    total_complete_lines = sum(line_counts.values())
    ban_line_rows = []
    for line_key, count in sorted(line_counts.items(), key=lambda item: item[1], reverse=True)[:16]:
        heroes = list(line_key)
        hero_profiles = [hero_lookup.get(hero, {}) for hero in heroes if hero]
        avg_comfort = round(
            sum(float(profile.get("comfort_rate", 0) or 0) for profile in hero_profiles) / len(hero_profiles),
            1,
        ) if hero_profiles else 0.0
        avg_adjusted_wr = round(
            sum(float(profile.get("adjusted_win_rate", 0) or 0) for profile in hero_profiles) / len(hero_profiles),
            1,
        ) if hero_profiles else 0.0
        line_wr = round((line_wins[line_key] / count) * 100, 1) if count else 0.0
        line_strength = round((avg_comfort * 0.55) + (avg_adjusted_wr * 0.45), 1)
        # DRAFT_SLOT_ORDER = (ban1, protect1, ban2, ban3, protect2, ban4)
        # Indices 0,2,3,5 = bans;  indices 1,4 = protects
        slot_labels = list(DRAFT_SLOT_ORDER)
        bans = [heroes[i] for i in (0, 2, 3, 5) if i < len(heroes) and heroes[i]]
        protects = [heroes[i] for i in (1, 4) if i < len(heroes) and heroes[i]]
        slots = [
            {"label": slot_labels[i], "hero": heroes[i], "type": "protect" if slot_labels[i].startswith("protect") else "ban"}
            for i in range(len(heroes)) if i < len(slot_labels)
        ]
        ban_line_rows.append(
            {
                "heroes": heroes,
                "bans": bans,
                "protects": protects,
                "slots": slots,
                "count": count,
                "rate": round((count / total_complete_lines) * 100, 1) if total_complete_lines else 0.0,
                "avg_comfort": avg_comfort,
                "avg_adjusted_win_rate": avg_adjusted_wr,
                "line_win_rate": line_wr,
                "line_strength": line_strength,
            }
        )

    total_comps = sum(comp_counts.values())
    comp_rows = []
    # Bayesian win-probability model over comp outcomes.
    # This provides a lightweight learned estimate that is robust for low-sample comps.
    prior_strength = 12.0
    alpha0 = max(1e-6, overall_wr * prior_strength + 1.0)
    beta0 = max(1e-6, (1.0 - overall_wr) * prior_strength + 1.0)
    for comp_key, count in sorted(comp_counts.items(), key=lambda item: item[1], reverse=True)[:16]:
        heroes = list(comp_key)
        hero_profiles = [hero_lookup.get(hero, {}) for hero in heroes if hero]
        avg_comfort = round(
            sum(float(profile.get("comfort_rate", 0) or 0) for profile in hero_profiles) / len(hero_profiles),
            1,
        ) if hero_profiles else 0.0
        avg_adjusted_wr = round(
            sum(float(profile.get("adjusted_win_rate", 0) or 0) for profile in hero_profiles) / len(hero_profiles),
            1,
        ) if hero_profiles else 0.0
        comp_wr = round((comp_wins[comp_key] / count) * 100, 1) if count else 0.0
        comp_wins_weighted = float(comp_wins.get(comp_key, 0.0) or 0.0)
        posterior_win_prob = (
            (comp_wins_weighted + alpha0) / (count + alpha0 + beta0)
            if count > 0 else overall_wr
        )
        posterior_strength = count + alpha0 + beta0
        ml_confidence = min(100.0, (count / (count + prior_strength)) * 100.0) if count > 0 else 0.0
        uncertainty_penalty = (100.0 / math.sqrt(max(1.0, posterior_strength)))
        ml_comp_score = max(0.0, min(100.0, posterior_win_prob * 100.0 - uncertainty_penalty))
        comp_strength_base = (avg_comfort * 0.5) + (avg_adjusted_wr * 0.5)
        direct_comp_trend_delta = comp_trend_deltas.get(comp_key, 0.0)
        hero_trend_values = [hero_trend_deltas.get(hero_name, 0.0) for hero_name in heroes if hero_name]
        hero_avg_trend_delta = (sum(hero_trend_values) / len(hero_trend_values)) if hero_trend_values else 0.0
        if abs(direct_comp_trend_delta) > 0.05:
            comp_trend_delta = (direct_comp_trend_delta * 0.7) + (hero_avg_trend_delta * 0.3)
        else:
            # Exact 6-hero comps can be sparse; fall back to member-hero trend signal.
            comp_trend_delta = hero_avg_trend_delta
        comp_trend_confidence = min(1.0, count / 8.0) if count else 0.0
        comp_trend_bonus = max(
            -MACHINE_COMP_TREND_CAP,
            min(MACHINE_COMP_TREND_CAP, comp_trend_delta * MACHINE_COMP_TREND_BLEND * comp_trend_confidence),
        )
        comp_strength = round(max(0.0, min(100.0, comp_strength_base + comp_trend_bonus)), 1)
        comp_rows.append(
            {
                "heroes": heroes,
                "count": count,
                "rate": round((count / total_comps) * 100, 1) if total_comps else 0.0,
                "avg_comfort": avg_comfort,
                "avg_adjusted_win_rate": avg_adjusted_wr,
                "comp_win_rate": comp_wr,
                "ml_win_prob": round(posterior_win_prob * 100, 1),
                "ml_confidence": round(ml_confidence, 1),
                "ml_comp_score": round(ml_comp_score, 1),
                "comp_direct_trend_delta_pp": round(direct_comp_trend_delta, 1),
                "comp_hero_avg_trend_delta_pp": round(hero_avg_trend_delta, 1),
                "comp_trend_delta_pp": round(comp_trend_delta, 1),
                "comp_trend_bonus": round(comp_trend_bonus, 1),
                "comp_strength_base": round(comp_strength_base, 1),
                "comp_strength": comp_strength,
            }
        )

    comp_rows.sort(
        key=lambda row: (
            float(row.get("comp_strength", 0) or 0),
            float(row.get("rate", 0) or 0),
            float(row.get("count", 0) or 0),
        ),
        reverse=True,
    )

    comp_path_rows = []
    equivalent_path_rows = []
    top_comp_keys = [tuple(row["heroes"]) for row in comp_rows[:10] if row.get("heroes")]
    for comp_key in top_comp_keys:
        path_counts = comp_to_line_counts.get(comp_key, {})
        path_total = sum(path_counts.values())
        if not path_total:
            continue
        ordered_paths = sorted(path_counts.items(), key=lambda item: item[1], reverse=True)
        primary_line_key = ordered_paths[0][0]
        top_paths = []
        for line_key, count in ordered_paths[:4]:
            line_parts = split_line(line_key)
            top_paths.append(
                {
                    "heroes": list(line_key),
                    "bans": line_parts["bans"],
                    "protects": line_parts["protects"],
                    "count": round(count, 2),
                    "rate": round((count / path_total) * 100, 1) if path_total else 0.0,
                    "distance_from_primary": line_distance(line_key, primary_line_key),
                }
            )

        stable_slots = 0
        for slot_idx in range(len(primary_line_key)):
            slot_values = {path_key[slot_idx] for path_key, _ in ordered_paths[:3] if len(path_key) > slot_idx}
            if len(slot_values) == 1:
                stable_slots += 1

        path_distances = [
            line_distance(primary_line_key, path_key)
            for path_key, _ in ordered_paths[1:4]
        ]
        comp_path_rows.append(
            {
                "heroes": list(comp_key),
                "path_count": len(ordered_paths),
                "top_path_share": round((ordered_paths[0][1] / path_total) * 100, 1) if path_total else 0.0,
                "stable_slots": stable_slots,
                "deviation_budget": max(path_distances) if path_distances else 0,
                "avg_deviation": round(sum(path_distances) / len(path_distances), 1) if path_distances else 0.0,
                "top_draft_paths": top_paths,
            }
        )

        if len(ordered_paths) > 1:
            alt_line_key, alt_count = ordered_paths[1]
            primary_parts = split_line(primary_line_key)
            alt_parts = split_line(alt_line_key)
            equivalent_path_rows.append(
                {
                    "heroes": list(comp_key),
                    "primary_bans": primary_parts["bans"],
                    "primary_protects": primary_parts["protects"],
                    "alt_bans": alt_parts["bans"],
                    "alt_protects": alt_parts["protects"],
                    "primary_rate": round((ordered_paths[0][1] / path_total) * 100, 1) if path_total else 0.0,
                    "alt_rate": round((alt_count / path_total) * 100, 1) if path_total else 0.0,
                    "path_distance": line_distance(primary_line_key, alt_line_key),
                }
            )

    volatile_hero_rows = []
    for hero_name, appearances in hero_lookup.items():
        with_maps = hero_presence_maps.get(hero_name, 0.0)
        if not with_maps or weighted_total_maps <= with_maps:
            continue
        with_wr = (hero_presence_wins.get(hero_name, 0.0) / with_maps) if with_maps else 0.0
        without_maps = weighted_total_maps - with_maps
        without_wins = weighted_total_wins - hero_presence_wins.get(hero_name, 0.0)
        without_wr = (without_wins / without_maps) if without_maps else overall_wr
        delta = round((with_wr - without_wr) * 100, 1)
        volatile_hero_rows.append(
            {
                "hero": hero_name,
                "with_maps": round(with_maps, 2),
                "with_wr": round(with_wr * 100, 1),
                "without_wr": round(without_wr * 100, 1),
                "delta": delta,
                "volatility": abs(delta),
                "favored_side": "Team" if delta > 0 else "Enemy" if delta < 0 else "Neutral",
            }
        )
    volatile_hero_rows.sort(
        key=lambda row: (row["volatility"], row["with_maps"], row["hero"].lower()),
        reverse=True,
    )

    pivot_rows = []
    for comp_key in top_comp_keys[:6]:
        comp_set = set(comp_key)
        pivot_options = []
        for other_key, other_count in comp_counts.items():
            if other_key == comp_key:
                continue
            shared_count = len(comp_set & set(other_key))
            if shared_count < 3:
                continue
            diff_count = len(set(comp_key) ^ set(other_key))
            pivot_options.append(
                {
                    "heroes": list(other_key),
                    "shared_count": shared_count,
                    "diff_count": diff_count,
                    "rate": round((other_count / total_comps) * 100, 1) if total_comps else 0.0,
                }
            )
        pivot_options.sort(
            key=lambda row: (row["shared_count"], -row["diff_count"], row["rate"]),
            reverse=True,
        )
        if pivot_options:
            pivot_rows.append(
                {
                    "base_heroes": list(comp_key),
                    "pivots": pivot_options[:3],
                }
            )

    mode_hero_rows = []
    for mode_name, counts in mode_hero_counts.items():
        mode_total = mode_totals.get(mode_name, 0.0)
        if not mode_total:
            continue
        for hero_name, count in counts.items():
            overall_share = float(hero_lookup.get(hero_name, {}).get("comfort_rate", 0) or 0)
            mode_share = (count / mode_total) * 100 if mode_total else 0.0
            delta = round(mode_share - overall_share, 1)
            mode_hero_rows.append(
                {
                    "mode": mode_name,
                    "hero": hero_name,
                    "mode_rate": round(mode_share, 1),
                    "overall_rate": round(overall_share, 1),
                    "delta": delta,
                }
            )
    mode_hero_rows.sort(
        key=lambda row: (abs(row["delta"]), row["mode_rate"], row["hero"].lower()),
        reverse=True,
    )

    mode_comp_rows = []
    overall_comp_rate_lookup = {
        tuple(row["heroes"]): float(row.get("rate", 0) or 0)
        for row in comp_rows
    }
    for mode_name, counts in mode_comp_counts.items():
        mode_total = mode_comp_totals.get(mode_name, 0.0)
        if not mode_total:
            continue
        for comp_key, count in counts.items():
            mode_rate = (count / mode_total) * 100 if mode_total else 0.0
            overall_rate = overall_comp_rate_lookup.get(comp_key, 0.0)
            delta = round(mode_rate - overall_rate, 1)
            mode_comp_rows.append(
                {
                    "mode": mode_name,
                    "heroes": list(comp_key),
                    "mode_rate": round(mode_rate, 1),
                    "overall_rate": round(overall_rate, 1),
                    "delta": delta,
                }
            )
    mode_comp_rows.sort(
        key=lambda row: (abs(row["delta"]), row["mode_rate"]),
        reverse=True,
    )

    map_state_rows = []
    for label, counts in sorted(map_state_counts.items(), key=lambda item: map_state_totals.get(item[0], 0), reverse=True):
        total = map_state_totals.get(label, 0.0)
        options = [
            {
                "map_name": map_name,
                "mode": MAP_MODES.get(map_name, "Other"),
                "count": round(count, 2),
                "rate": round((count / total) * 100, 1) if total else 0.0,
            }
            for map_name, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:8]
        ]
        map_state_rows.append(
            {
                "label": label,
                "sample_weight": round(total, 2),
                "options": options,
            }
        )

    return {
        "status": "ready" if total_maps else "empty",
        "training_maps": total_maps,
        "weighted_maps": round(weighted_total_maps, 2),
        "uses_exponential_recency": True,
        "uses_weighted_linear_trend": True,
        "model_methods": {
            "recency_weighting": "exponential_decay",
            "trend_estimator": "weighted_linear_regression",
            "comp_outcome_model": "bayesian_beta_binomial",
        },
        "overall_win_rate": round(overall_wr * 100, 1),
        "comfort_core_rows": comfort_core_rows,
        "hero_pool_rows": hero_pool_rows,
        "player_hero_rows": player_hero_rows,
        "ban_line_rows": ban_line_rows,
        "comp_rows": comp_rows,
        "comp_path_rows": comp_path_rows[:8],
        "volatile_hero_rows": volatile_hero_rows[:12],
        "pivot_rows": pivot_rows[:6],
        "mode_hero_rows": mode_hero_rows[:10],
        "mode_comp_rows": mode_comp_rows[:8],
        "equivalent_path_rows": equivalent_path_rows[:8],
        "map_state_rows": map_state_rows,
        "map_type_weights": {"Standard": 1.0, "PTW": 1.75, "Test": 0.55},
        "recency_half_life_days": RECENCY_HALFLIFE_DAYS,
        "trend_model": {
            "min_points": MACHINE_TREND_MIN_POINTS,
            "hero_blend": MACHINE_HERO_TREND_BLEND,
            "comp_blend": MACHINE_COMP_TREND_BLEND,
            "hero_cap": MACHINE_HERO_TREND_CAP,
            "comp_cap": MACHINE_COMP_TREND_CAP,
        },
    }


