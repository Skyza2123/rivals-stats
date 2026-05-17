# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def build_prep_draft_correlation_bundle(prep_scrims: list[dict]) -> dict:
    by_kind = {
        "ban": {"counts": defaultdict(int), "drafts": []},
        "protect": {"counts": defaultdict(int), "drafts": []},
    }

    for scrim in prep_scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            enemy_slot = "team2" if our_team_slot == "team1" else "team1"
            map_outcome = get_map_outcome_for_slot(map_entry, our_team_slot)
            draft = map_entry.get("draft", {})
            enemy_draft = draft.get(enemy_slot, {}) if isinstance(draft, dict) else {}

            for slot_kind in ("ban", "protect"):
                picked_heroes = sorted(
                    {
                        _canonical_draft_hero(hero_name)
                        for slot_key, hero_name in enemy_draft.items()
                        if slot_kind in slot_key and _canonical_draft_hero(hero_name)
                    }
                )
                if not picked_heroes:
                    continue
                by_kind[slot_kind]["drafts"].append({"heroes": picked_heroes, "outcome": map_outcome})
                for hero_name in picked_heroes:
                    by_kind[slot_kind]["counts"][hero_name] += 1

    def _finalize(
        kind_data: dict,
        *,
        limit: int = 8,
        partner_limit: int = 4,
        group_limit: int = 8,
        lock_threshold: float = 90.0,
    ) -> dict:
        enemy_pick_counts = kind_data["counts"]
        draft_rows = kind_data["drafts"]
        draft_count = len(draft_rows)
        locked_hero_keys = {
            hero_name
            for hero_name, pick_count in enemy_pick_counts.items()
            if draft_count and round((pick_count / draft_count) * 100, 1) >= lock_threshold
        }
        locked_rows = [
            {
                "hero": hero_name,
                "count": pick_count,
                "rate": round((pick_count / draft_count) * 100, 1) if draft_count else 0,
                "low_sample": draft_count < 3,
            }
            for hero_name, pick_count in enemy_pick_counts.items()
            if hero_name in locked_hero_keys
        ]
        locked_rows.sort(key=lambda row: (row["rate"], row["count"], row["hero"]), reverse=True)

        eligible_pick_counts = {
            hero_name: pick_count
            for hero_name, pick_count in enemy_pick_counts.items()
            if hero_name not in locked_hero_keys
        }
        co_pick_counts = defaultdict(lambda: defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0}))
        group_counts = {
            2: defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0}),
            3: defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0}),
            4: defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0}),
        }
        for draft_row in draft_rows:
            picked_heroes = draft_row["heroes"]
            outcome = draft_row.get("outcome", "")
            co_heroes = [hero_name for hero_name in picked_heroes if hero_name not in locked_hero_keys]
            for hero_name in co_heroes:
                for partner_hero in co_heroes:
                    if partner_hero != hero_name:
                        co_pick_counts[hero_name][partner_hero]["count"] += 1
                        if outcome == "Win":
                            co_pick_counts[hero_name][partner_hero]["wins"] += 1
                        elif outcome == "Loss":
                            co_pick_counts[hero_name][partner_hero]["losses"] += 1
            for group_size in (2, 3, 4):
                if len(co_heroes) < group_size:
                    continue
                for group_key in combinations(co_heroes, group_size):
                    group_counts[group_size][group_key]["count"] += 1
                    if outcome == "Win":
                        group_counts[group_size][group_key]["wins"] += 1
                    elif outcome == "Loss":
                        group_counts[group_size][group_key]["losses"] += 1

        rows = []
        for hero_name, pick_count in eligible_pick_counts.items():
            partner_rows = []
            for partner_hero, co_stats in co_pick_counts[hero_name].items():
                partner_pick_count = eligible_pick_counts.get(partner_hero, 0)
                if not partner_pick_count:
                    continue
                co_count = co_stats["count"]
                decided = co_stats["wins"] + co_stats["losses"]
                partner_rows.append(
                    {
                        "hero": partner_hero,
                        "count": partner_pick_count,
                        "co_count": co_count,
                        "wins": co_stats["wins"],
                        "losses": co_stats["losses"],
                        "win_rate": round((co_stats["wins"] / decided) * 100, 1) if decided else None,
                        "correlation": round((co_count / pick_count) * 100, 1) if pick_count else 0,
                        "mutual_correlation": round(
                            ((co_count / pick_count) * (co_count / partner_pick_count)) ** 0.5 * 100,
                            1,
                        ) if pick_count and partner_pick_count else 0,
                    }
                )
            partner_rows.sort(key=lambda row: (row["co_count"], row["correlation"], row["count"]), reverse=True)
            if not partner_rows:
                continue
            rows.append(
                {
                    "hero": hero_name,
                    "count": pick_count,
                    "rate": round((pick_count / draft_count) * 100, 1) if draft_count else 0,
                    "partners": partner_rows[:partner_limit],
                }
            )

        rows.sort(key=lambda row: (row["count"], row["rate"], len(row["partners"])), reverse=True)
        group_rows_by_size = {}
        for group_size, groups in group_counts.items():
            group_rows = []
            for heroes, stats in groups.items():
                decided = stats["wins"] + stats["losses"]
                group_rows.append(
                    {
                        "heroes": list(heroes),
                        "count": stats["count"],
                        "wins": stats["wins"],
                        "losses": stats["losses"],
                        "win_rate": round((stats["wins"] / decided) * 100, 1) if decided else None,
                    }
                )
            group_rows.sort(key=lambda row: (row["count"], row["wins"]), reverse=True)
            group_rows_by_size[group_size] = group_rows[:group_limit]

        return {
            "locked_rows": locked_rows,
            "cooccurrence_rows": rows[:limit],
            "group_rows_by_size": group_rows_by_size,
            "draft_count": draft_count,
            "lock_threshold": lock_threshold,
        }

    return {
        "ban": _finalize(by_kind["ban"]),
        "protect": _finalize(by_kind["protect"]),
    }


def build_prep_expected_comp_plan(prep_scrims: list[dict], team_players: list[sqlite3.Row | dict], prep_analytics: dict, all_scrims: list[dict] | None = None) -> dict:
    pair_counts = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    hero_counts = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    comp_variant_counts = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})

    players = [
        {
            "name": (row["name"] if isinstance(row, sqlite3.Row) else row.get("name", "")).strip(),
            "role": (row["role"] if isinstance(row, sqlite3.Row) else row.get("role", "")).strip(),
            "main_hero": (row["main_hero"] if isinstance(row, sqlite3.Row) else row.get("main_hero", "")).strip(),
        }
        for row in team_players
        if (row["name"] if isinstance(row, sqlite3.Row) else row.get("name", "")).strip()
    ]
    roster_name_lookup = {
        player["name"].lower(): player["name"]
        for player in players
        if player.get("name")
    }

    player_by_main_hero = defaultdict(list)
    for player in players:
        main_hero = _canonical_draft_hero(player["main_hero"])
        if main_hero:
            player_by_main_hero[main_hero].append(player)

    for scrim in prep_scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            result = get_map_outcome_for_slot(map_entry, our_team_slot)

            map_pairs: list[tuple[str, str]] = []
            map_heroes: list[str] = []
            largest_lineup: list[str] = []

            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue

                lineup = []
                for slot in section.get(our_team_slot, []):
                    if not isinstance(slot, dict):
                        continue
                    hero_name = _canonical_draft_hero(slot.get("hero", ""))
                    if not hero_name:
                        continue

                    lineup.append(hero_name)
                    map_heroes.append(hero_name)

                    player_name = (slot.get("player", "") or "").strip()
                    if player_name:
                        roster_player_name = roster_name_lookup.get(player_name.lower())
                        if roster_player_name:
                            map_pairs.append((hero_name, roster_player_name))

                if len(lineup) > len(largest_lineup):
                    largest_lineup = lineup

            for hero_name in map_heroes:
                hero_counts[hero_name]["count"] += 1
                if result == "Win":
                    hero_counts[hero_name]["wins"] += 1
                elif result == "Loss":
                    hero_counts[hero_name]["losses"] += 1

            for hero_name, player_name in map_pairs:
                pair_counts[(hero_name, player_name)]["count"] += 1
                if result == "Win":
                    pair_counts[(hero_name, player_name)]["wins"] += 1
                elif result == "Loss":
                    pair_counts[(hero_name, player_name)]["losses"] += 1

            if largest_lineup:
                lineup_key = tuple(sorted(largest_lineup))
                comp_variant_counts[lineup_key]["count"] += 1
                if result == "Win":
                    comp_variant_counts[lineup_key]["wins"] += 1
                elif result == "Loss":
                    comp_variant_counts[lineup_key]["losses"] += 1

    # Build a full pair_counts from ALL scrims (not just vs this enemy) so that
    # player capability lookups have enough history even when enemy-filtered data is thin.
    full_pair_counts: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    for _scrim in (all_scrims or prep_scrims):
        for _map in _scrim.get("maps", []):
            _our_slot = _map.get("our_team_slot", "team1")
            if _our_slot not in TEAM_SLOTS:
                _our_slot = "team1"
            for _section in _map.get("comp", []):
                if not isinstance(_section, dict):
                    continue
                for _slot in _section.get(_our_slot, []):
                    if not isinstance(_slot, dict):
                        continue
                    _hero = _canonical_draft_hero(_slot.get("hero", ""))
                    _player = (_slot.get("player", "") or "").strip()
                    if _hero and _player:
                        _roster_pname = roster_name_lookup.get(_player.lower())
                        if _roster_pname:
                            full_pair_counts[(_hero, _roster_pname)]["count"] += 1

    # Determine each player's main heroes (top played, min 2 maps, up to 3).
    # Falls back to single most-played if none meet the 2-map threshold.
    player_main_heroes: dict[str, list[str]] = {}
    for player_obj in players:
        pname = player_obj["name"]
        player_hero_counts = sorted(
            [
                (hero_name, stats["count"])
                for (hero_name, player_name), stats in pair_counts.items()
                if player_name == pname
            ],
            key=lambda x: x[1],
            reverse=True,
        )
        if player_hero_counts:
            mains = [h for h, c in player_hero_counts if c >= 2][:3]
            if not mains:
                mains = [player_hero_counts[0][0]]
            player_main_heroes[pname] = mains
        elif player_obj.get("main_hero"):
            player_main_heroes[pname] = [_canonical_draft_hero(player_obj["main_hero"])]

    # Second pass: for each map where any of a player's main heroes was enemy-banned,
    # track what that player actually switched to and the outcome.
    # Key is (player_name, banned_main_hero).
    player_ban_pivot_counts: dict[tuple, dict] = {
        (pname, main_h): defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
        for pname, mains in player_main_heroes.items()
        for main_h in mains
    }
    player_main_ban_total: dict[tuple, int] = defaultdict(int)

    for scrim in prep_scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            pivot_result = get_map_outcome_for_slot(map_entry, our_team_slot)
            enemy_slot = "team2" if our_team_slot == "team1" else "team1"
            draft = map_entry.get("draft", {})
            enemy_draft = draft.get(enemy_slot, {}) if isinstance(draft, dict) else {}
            enemy_bans = {
                _canonical_draft_hero(v)
                for k, v in enemy_draft.items()
                if "ban" in k and _canonical_draft_hero(v)
            }

            # Build map: roster player -> section-counted heroes they played this map.
            # A ban-pivot row should count one replacement per map, not every
            # short section swap the player touched after their main was banned.
            player_heroes_this_map: dict[str, Counter[str]] = defaultdict(Counter)
            player_hero_first_seen_this_map: dict[str, dict[str, int]] = defaultdict(dict)
            for section_index, section in enumerate(map_entry.get("comp", [])):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(our_team_slot, []):
                    if not isinstance(slot, dict):
                        continue
                    h = _canonical_draft_hero(slot.get("hero", ""))
                    p = (slot.get("player", "") or "").strip()
                    if h and p:
                        roster_pname = roster_name_lookup.get(p.lower())
                        if roster_pname:
                            player_heroes_this_map[roster_pname][h] += 1
                            player_hero_first_seen_this_map[roster_pname].setdefault(h, section_index)

            for pname, mains in player_main_heroes.items():
                for main_h in mains:
                    if main_h in enemy_bans:
                        player_main_ban_total[(pname, main_h)] += 1
                        pivot_candidates = [
                            (hero_name, count)
                            for hero_name, count in player_heroes_this_map.get(pname, {}).items()
                            if hero_name != main_h
                        ]
                        if not pivot_candidates:
                            continue
                        first_seen = player_hero_first_seen_this_map.get(pname, {})
                        pivot_candidates.sort(
                            key=lambda item: (-item[1], first_seen.get(item[0], 9999), item[0])
                        )
                        pivot_hero = pivot_candidates[0][0]
                        player_ban_pivot_counts[(pname, main_h)][pivot_hero]["count"] += 1
                        if pivot_result == "Win":
                            player_ban_pivot_counts[(pname, main_h)][pivot_hero]["wins"] += 1
                        elif pivot_result == "Loss":
                            player_ban_pivot_counts[(pname, main_h)][pivot_hero]["losses"] += 1

    player_pivot_rows: list[dict] = []
    for pname, mains in player_main_heroes.items():
        for main_h in mains:
            main_stats = pair_counts.get((main_h, pname), {"count": 0, "wins": 0, "losses": 0})
            main_maps = main_stats["count"]
            if main_maps == 0:
                continue
            main_wr = round((main_stats["wins"] / main_maps) * 100, 1)
            banned_maps = player_main_ban_total.get((pname, main_h), 0)
            pivot_counts = player_ban_pivot_counts.get((pname, main_h), {})
            pivot_hero = ""
            pivot_maps = 0
            pivot_wr = 0.0
            if pivot_counts:
                top_key, top_stats = max(pivot_counts.items(), key=lambda x: (x[1]["count"], x[1]["wins"]))
                pivot_hero = top_key
                pivot_maps = top_stats["count"]
                pivot_wr = round((top_stats["wins"] / pivot_maps) * 100, 1) if pivot_maps else 0.0
            player_pivot_rows.append({
                "player_name": pname,
                "main_hero": main_h,
                "main_hero_maps": main_maps,
                "main_hero_win_rate": main_wr,
                "banned_maps": banned_maps,
                "pivot_hero": pivot_hero,
                "pivot_maps": pivot_maps,
                "pivot_win_rate": pivot_wr,
            })
    # Sort by player name, then by main hero play count descending within each player
    player_pivot_rows.sort(key=lambda r: (r["player_name"].lower(), -r["main_hero_maps"]))

    def choose_player_for_hero(hero_name: str, used_names: set[str] | None = None) -> dict:
        used_names = used_names or set()
        candidates = [
            (player_name, stats)
            for (pair_hero, player_name), stats in pair_counts.items()
            if pair_hero == hero_name and player_name not in used_names
        ]
        candidates.sort(key=lambda row: (row[1]["count"], row[1]["wins"]), reverse=True)
        if candidates:
            top_name, top_stats = candidates[0]
            return {
                "name": top_name,
                "maps": top_stats["count"],
                "win_rate": round((top_stats["wins"] / top_stats["count"]) * 100, 1) if top_stats["count"] else 0,
                "source": "history",
            }

        # Fallback: check all-time pair counts (not enemy-filtered) for a richer history
        full_candidates = [
            (player_name, stats)
            for (pair_hero, player_name), stats in full_pair_counts.items()
            if pair_hero == hero_name and player_name not in used_names
        ]
        full_candidates.sort(key=lambda row: row[1]["count"], reverse=True)
        if full_candidates:
            top_name, top_stats = full_candidates[0]
            return {
                "name": top_name,
                "maps": top_stats["count"],
                "win_rate": 0,
                "source": "history_full",
            }

        hero_main_candidates = [player for player in player_by_main_hero.get(hero_name, []) if player["name"] not in used_names]
        if hero_main_candidates:
            pick = hero_main_candidates[0]
            return {
                "name": pick["name"],
                "maps": 0,
                "win_rate": 0,
                "source": "main_hero",
            }

        role_name = _hero_role(hero_name)
        role_candidates = [
            player for player in players
            if player["name"] not in used_names and role_name and player["role"].lower() == role_name.lower()
        ]
        if role_candidates:
            pick = role_candidates[0]
            return {
                "name": pick["name"],
                "maps": 0,
                "win_rate": 0,
                "source": "role_fit",
            }

        fallback = next((player for player in players if player["name"] not in used_names), None)
        if fallback is not None:
            return {
                "name": fallback["name"],
                "maps": 0,
                "win_rate": 0,
                "source": "fallback",
            }

        return {
            "name": "TBD",
            "maps": 0,
            "win_rate": 0,
            "source": "unassigned",
        }

    expected_hero_pool = [row["hero"] for row in prep_analytics.get("hero_rows", []) if row.get("hero")]
    if not expected_hero_pool:
        expected_hero_pool = [hero for hero, _stats in sorted(hero_counts.items(), key=lambda item: item[1]["count"], reverse=True)]
    expected_hero_pool = expected_hero_pool[:6]

    # Enforce minimum 2 per role (2 Vanguard, 2 Duelist, 2 Strategist)
    # unless triple support is very prominent (>35% of comp appearances)
    triple_support_appearances = sum(
        s["count"]
        for comp_key, s in comp_variant_counts.items()
        if sum(1 for h in comp_key if _hero_role(h) == "Strategist") >= 3
    )
    total_comp_appearances = sum(s["count"] for s in comp_variant_counts.values())
    triple_support_prominent = (
        total_comp_appearances > 0
        and (triple_support_appearances / total_comp_appearances) > 0.35
    )
    role_mins = {"Vanguard": 2, "Duelist": 2, "Strategist": 2}
    if triple_support_prominent:
        role_mins["Vanguard"] = 1

    pool_set = {_canonical_draft_hero(h) for h in expected_hero_pool}
    pool_by_role: dict[str, list[str]] = {}
    for h in expected_hero_pool:
        pool_by_role.setdefault(_hero_role(h), []).append(h)

    for role, min_needed in role_mins.items():
        deficit = min_needed - len(pool_by_role.get(role, []))
        if deficit > 0:
            role_candidates = sorted(
                [
                    (h, s["count"])
                    for h, s in hero_counts.items()
                    if _hero_role(h) == role and _canonical_draft_hero(h) not in pool_set
                ],
                key=lambda x: x[1],
                reverse=True,
            )
            for hero, _ in role_candidates[:deficit]:
                expected_hero_pool.append(hero)
                pool_set.add(_canonical_draft_hero(hero))
                pool_by_role.setdefault(role, []).append(hero)

    # Cap the pool to 6 heroes. If role enforcement caused it to exceed 6,
    # trim excess heroes from the most over-represented roles.
    while len(expected_hero_pool) > 6:
        role_counts: dict[str, int] = {}
        for h in expected_hero_pool:
            r = _hero_role(h)
            role_counts[r] = role_counts.get(r, 0) + 1
        # Remove the last hero whose role exceeds its minimum
        trimmed = False
        for i in range(len(expected_hero_pool) - 1, -1, -1):
            r = _hero_role(expected_hero_pool[i])
            if role_counts.get(r, 0) > role_mins.get(r, 0):
                del expected_hero_pool[i]
                trimmed = True
                break
        if not trimmed:
            # All roles are at or below their minimums; just drop the last entry
            expected_hero_pool.pop()

    expected_core = []
    used_names = set()
    for hero_name in expected_hero_pool:
        hero_stats = hero_counts.get(hero_name, {"count": 0, "wins": 0, "losses": 0})
        assignment = choose_player_for_hero(hero_name, used_names)
        if assignment.get("name") and assignment["name"] != "TBD":
            used_names.add(assignment["name"])

        expected_core.append(
            {
                "hero": hero_name,
                "role": _hero_role(hero_name),
                "maps": hero_stats["count"],
                "win_rate": round((hero_stats["wins"] / hero_stats["count"]) * 100, 1) if hero_stats["count"] else 0,
                "player": assignment,
            }
        )

    # Build lenient 5-hero core combos: groups all comps that share 5 heroes,
    # tolerating 1-hero flex pick variation.  Also track what the 6th (flex) pick
    # was across all those maps so we can surface "or <alt>" suggestions.
    combo5_counts: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    combo5_flex_counts: dict[tuple, dict] = defaultdict(lambda: defaultdict(int))

    for heroes, stats in comp_variant_counts.items():
        if len(heroes) < 5:
            continue
        for combo_key in combinations(heroes, 5):
            combo5_counts[combo_key]["count"] += stats["count"]
            combo5_counts[combo_key]["wins"] += stats["wins"]
            combo5_counts[combo_key]["losses"] += stats["losses"]
            if len(heroes) == 6:
                flex_heroes = set(heroes) - set(combo_key)
                for flex_h in flex_heroes:
                    combo5_flex_counts[combo_key][flex_h] += stats["count"]

    expected_comp_variants = []
    seen_comp_sets: list[frozenset] = []
    # Sort primarily by sample size; win rate only breaks ties between equally-played cores.
    # A comp with 8 maps at 50% WR is more reliable than one with 2 maps at 100% WR.
    sorted_5combos = sorted(
        combo5_counts.items(),
        key=lambda row: (
            row[1]["count"],
            round((row[1]["wins"] / row[1]["count"]) * 100) if row[1]["count"] else 0,
        ),
        reverse=True,
    )
    for core_heroes, stats in sorted_5combos:
        if len(expected_comp_variants) >= 3:
            break
        # Skip if this core overlaps heavily (>=4 shared heroes) with an already-shown comp.
        core_set = frozenset(core_heroes)
        if any(len(core_set & s) >= 4 for s in seen_comp_sets):
            continue
        seen_comp_sets.append(core_set)

        used_variant_names = set()
        slots = []
        for hero_name in core_heroes:
            assignment = choose_player_for_hero(hero_name, used_variant_names)
            if assignment.get("name") and assignment["name"] != "TBD":
                used_variant_names.add(assignment["name"])
            slots.append(
                {
                    "hero": hero_name,
                    "role": _hero_role(hero_name),
                    "player": assignment,
                }
            )

        flex_sorted = sorted(combo5_flex_counts[core_heroes].items(), key=lambda x: x[1], reverse=True)
        flex_hero = flex_sorted[0][0] if flex_sorted else ""
        flex_alt = flex_sorted[1][0] if len(flex_sorted) > 1 else ""
        flex_assignment = choose_player_for_hero(flex_hero, set(used_variant_names)) if flex_hero else {}
        flex_alt_assignment = choose_player_for_hero(flex_alt, set()) if flex_alt else {}

        expected_comp_variants.append(
            {
                "label": f"Expected Comp {len(expected_comp_variants) + 1}",
                "heroes": slots,
                "flex_hero": flex_hero,
                "flex_hero_player": flex_assignment,
                "flex_alt": flex_alt,
                "flex_alt_player": flex_alt_assignment,
                "maps": stats["count"],
                "win_rate": round((stats["wins"] / stats["count"]) * 100, 1) if stats["count"] else 0,
            }
        )

    combo4_counts = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    combo4_flex_counts: dict[tuple, dict] = defaultdict(lambda: defaultdict(int))
    for heroes, stats in comp_variant_counts.items():
        if len(heroes) < 4:
            continue
        for combo_key in combinations(heroes, 4):
            combo4_counts[combo_key]["count"] += stats["count"]
            combo4_counts[combo_key]["wins"] += stats["wins"]
            combo4_counts[combo_key]["losses"] += stats["losses"]
            flex_heroes = set(heroes) - set(combo_key)
            for flex_h in flex_heroes:
                combo4_flex_counts[combo_key][flex_h] += stats["count"]

    four_hero_combos = []
    seen_combo4_sets: list[frozenset] = []
    sorted_combo4 = sorted(
        combo4_counts.items(),
        key=lambda row: (
            row[1]["count"],
            round((row[1]["wins"] / row[1]["count"]) * 100) if row[1]["count"] else 0,
        ),
        reverse=True,
    )
    for heroes, stats in sorted_combo4:
        if len(four_hero_combos) >= 6:
            break
        combo_set = frozenset(heroes)
        # Skip if 2+ heroes overlap with an already-shown combo (keeps cores mostly distinct)
        if any(len(combo_set & s) >= 2 for s in seen_combo4_sets):
            continue
        seen_combo4_sets.append(combo_set)
        flex_sorted = sorted(combo4_flex_counts[heroes].items(), key=lambda x: x[1], reverse=True)
        flex_picks = [h for h, _ in flex_sorted[:2]]
        four_hero_combos.append(
            {
                "heroes": list(heroes),
                "flex": flex_picks,
                "maps": stats["count"],
                "win_rate": round((stats["wins"] / stats["count"]) * 100, 1) if stats["count"] else 0,
            }
        )

    top_enemy_bans = prep_analytics.get("enemy_ban_rows", [])[:4]
    suggested_adjustments = []
    core_hero_keys = {_canonical_draft_hero(item["hero"]) for item in expected_core}
    used_replacement_keys: set[str] = set()

    for row in top_enemy_bans:
        banned_hero = row.get("hero", "")
        banned_key = _canonical_draft_hero(banned_hero)

        # Who in our expected core plays this hero (if anyone)?
        impacted_slot = next(
            (item for item in expected_core if _canonical_draft_hero(item["hero"]) == banned_key),
            None,
        )
        impacted_player_name = impacted_slot.get("player", {}).get("name", "") if impacted_slot else ""

        banned_role = _hero_role(banned_key)

        replacement_hero = ""
        replacement_player_name = ""

        # Only suggest a swap if the banned hero is actually in our expected core.
        # If the enemy bans something we don't run, there's nothing to swap.
        if impacted_slot is not None:
            # Primary: what did this player actually play on maps where the banned hero was banned?
            # player_ban_pivot_counts[(player, banned_hero)] -> {hero: {count, wins, losses}}
            if impacted_player_name:
                ban_pivot = player_ban_pivot_counts.get((impacted_player_name, banned_key), {})
                ban_pivot_alts = sorted(
                    [
                        (hero_name, stats)
                        for hero_name, stats in ban_pivot.items()
                        if _canonical_draft_hero(hero_name) not in core_hero_keys
                        and _canonical_draft_hero(hero_name) not in used_replacement_keys
                        and _canonical_draft_hero(hero_name) != banned_key
                    ],
                    key=lambda x: (x[1]["count"], x[1]["wins"]),
                    reverse=True,
                )
                if ban_pivot_alts:
                    # Best case: we saw this player swap to something when banned.
                    replacement_hero = ban_pivot_alts[0][0]
                    replacement_player_name = impacted_player_name
                    used_replacement_keys.add(_canonical_draft_hero(replacement_hero))
                else:
                    # Fallback: any same-role hero the player has played historically.
                    # Prefer enemy-filtered pair_counts; supplement with full_pair_counts
                    # so thin enemy samples don't leave players with no suggestion.
                    _capability_counts = full_pair_counts if full_pair_counts else pair_counts
                    player_role_alts = sorted(
                        [
                            (hero_name, stats)
                            for (hero_name, player_name), stats in _capability_counts.items()
                            if player_name == impacted_player_name
                            and _hero_role(hero_name) == banned_role
                            and _canonical_draft_hero(hero_name) not in core_hero_keys
                            and _canonical_draft_hero(hero_name) not in used_replacement_keys
                            and _canonical_draft_hero(hero_name) != banned_key
                        ],
                        key=lambda x: (x[1]["count"], x[1]["wins"]),
                        reverse=True,
                    )
                    if player_role_alts:
                        replacement_hero = player_role_alts[0][0]
                        replacement_player_name = impacted_player_name
                        used_replacement_keys.add(_canonical_draft_hero(replacement_hero))
                    else:
                        # No play history for this player on other same-role heroes.
                        # Check their declared main_hero in the roster as a candidate.
                        roster_player = next(
                            (p for p in players if p["name"] == impacted_player_name),
                            None,
                        )
                        roster_main = _canonical_draft_hero(roster_player["main_hero"]) if roster_player else ""
                        if (
                            roster_main
                            and roster_main != banned_key
                            and _hero_role(roster_main) == banned_role
                            and roster_main not in core_hero_keys
                            and roster_main not in used_replacement_keys
                        ):
                            replacement_hero = roster_main
                            replacement_player_name = impacted_player_name
                            used_replacement_keys.add(roster_main)
                        else:
                            # No history and no viable main_hero - still keep the same player.
                            # They're the one who needs to adapt; we just have no hero suggestion.
                            replacement_player_name = impacted_player_name

            # Fallback: only if we still have no player identified at all, search any
            # player who has played a same-role hero not already in core.
            if not replacement_player_name:
                role_candidates = sorted(
                    [
                        (h, s)
                        for h, s in hero_counts.items()
                        if _hero_role(h) == banned_role
                        and _canonical_draft_hero(h) not in core_hero_keys
                        and _canonical_draft_hero(h) not in used_replacement_keys
                        and _canonical_draft_hero(h) != banned_key
                    ],
                    key=lambda x: (x[1]["count"], x[1]["wins"]),
                    reverse=True,
                )
                if role_candidates:
                    replacement_hero = role_candidates[0][0]
                    used_replacement_keys.add(_canonical_draft_hero(replacement_hero))
                    replace_assign = choose_player_for_hero(replacement_hero)
                    replacement_player_name = replace_assign.get("name", "")

        suggested_adjustments.append(
            {
                "banned_hero": banned_hero,
                "ban_rate": row.get("ban_rate", 0),
                "impacted_player_name": impacted_player_name,
                "replacement_hero": replacement_hero,
                "replacement_player_name": replacement_player_name,
            }
        )

    hero_player_differences = []
    hero_player_rows: dict[str, list[dict]] = defaultdict(list)
    for (hero_name, player_name), stats in pair_counts.items():
        count = int(stats.get("count", 0) or 0)
        if not hero_name or not player_name or count <= 0:
            continue
        wins = int(stats.get("wins", 0) or 0)
        losses = int(stats.get("losses", 0) or 0)
        decided = wins + losses
        hero_player_rows[hero_name].append(
            {
                "player_name": player_name,
                "maps": count,
                "wins": wins,
                "losses": losses,
                "decided_maps": decided,
                "win_rate": round((wins / decided) * 100, 1) if decided else 0,
            }
        )

    for hero_name, rows in hero_player_rows.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda row: (row["maps"], row["wins"]), reverse=True)
        best = rows[0]
        alt = rows[1]
        hero_player_differences.append(
            {
                "hero": hero_name,
                "best_player": best,
                "alt_player": alt,
                "win_rate_diff": round(best["win_rate"] - alt["win_rate"], 1),
                "sample_total": best["maps"] + alt["maps"],
                "all_players": rows,
            }
        )

    hero_player_differences.sort(
        key=lambda row: (abs(row["win_rate_diff"]), row["sample_total"]),
        reverse=True,
    )

    return {
        "expected_core": expected_core,
        "expected_comp_variants": expected_comp_variants,
        "four_hero_combos": four_hero_combos,
        "suggested_adjustments": suggested_adjustments,
        "hero_player_differences": hero_player_differences[:12],
        "player_pivot_rows": player_pivot_rows,
    }


def build_team_prep_context(
    *,
    team_scrims: list[dict],
    team_players: list[sqlite3.Row | dict],
    enemy_teams: list[dict],
    selected_enemy_id_raw: str,
    compare_map_a_raw: str,
    compare_map_b_raw: str,
) -> dict:
    roster_player_names = [
        (row["name"] if isinstance(row, sqlite3.Row) else row.get("name", ""))
        for row in team_players
        if (row["name"] if isinstance(row, sqlite3.Row) else row.get("name", "")).strip()
    ]
    enemy_lookup = {str(enemy["id"]): enemy for enemy in enemy_teams}
    selected_enemy_id = selected_enemy_id_raw.strip()
    selected_enemy = enemy_lookup.get(selected_enemy_id)

    prep_scrims = team_scrims
    selected_enemy_name = ""
    if selected_enemy is not None:
        selected_enemy_name = selected_enemy["name"]
        prep_scrims = filter_team_scrims_for_enemy(team_scrims, int(selected_enemy_id), selected_enemy_name)

    prep_analytics = build_scrim_analytics(prep_scrims, roster_player_names=roster_player_names)
    draft_phase_timeline = build_draft_phase_timeline(prep_scrims)
    prep_expected_plan = build_prep_expected_comp_plan(prep_scrims, team_players, prep_analytics, all_scrims=team_scrims)
    prep_draft_correlation = build_prep_draft_correlation_bundle(prep_scrims)
    prep_ban_correlation = prep_draft_correlation["ban"]
    prep_protect_correlation = prep_draft_correlation["protect"]

    compare_map_options = [row["map_name"] for row in draft_phase_timeline.get("maps", [])]
    compare_map_a = (compare_map_a_raw or "").strip()
    if compare_map_a not in compare_map_options:
        compare_map_a = compare_map_options[0] if compare_map_options else ""

    remaining_compare_maps = [map_name for map_name in compare_map_options if map_name != compare_map_a]
    compare_map_b = (compare_map_b_raw or "").strip()
    if compare_map_b not in remaining_compare_maps:
        compare_map_b = remaining_compare_maps[0] if remaining_compare_maps else ""

    compare_map_rows = build_draft_phase_map_comparison_rows(
        draft_phase_timeline,
        compare_map_a,
        compare_map_b,
    )

    return {
        "prep_analytics": prep_analytics,
        "prep_scrim_count": len(prep_scrims),
        "selected_prep_enemy_id": selected_enemy_id,
        "selected_prep_enemy_name": selected_enemy_name,
        "compare_map_options": compare_map_options,
        "compare_map_a": compare_map_a,
        "compare_map_b": compare_map_b,
        "compare_map_rows": compare_map_rows,
        "draft_phase_timeline": draft_phase_timeline,
        "prep_expected_plan": prep_expected_plan,
        "prep_ban_correlation": prep_ban_correlation,
        "prep_ban_correlation_rows": prep_ban_correlation["cooccurrence_rows"],
        "prep_ban_group_rows_by_size": prep_ban_correlation["group_rows_by_size"],
        "prep_perma_ban_rows": prep_ban_correlation["locked_rows"],
        "prep_protect_correlation": prep_protect_correlation,
        "prep_protect_correlation_rows": prep_protect_correlation["cooccurrence_rows"],
        "prep_perma_protect_rows": prep_protect_correlation["locked_rows"],
    }


def _sanitize_simulator_draft_slots(raw_slots: dict | None) -> dict[str, str]:
    cleaned = {slot_name: "" for slot_name in SIMULATOR_SLOT_ORDER}
    if not isinstance(raw_slots, dict):
        return cleaned

    for slot_name in SIMULATOR_SLOT_ORDER:
        cleaned[slot_name] = _canonical_draft_hero(raw_slots.get(slot_name, ""))
    return cleaned


def _sanitize_one_sided_concept_slots(raw_slots: dict | None) -> dict[str, str]:
    cleaned = {slot_name: "" for slot_name in CONCEPT_ONE_SIDED_SLOT_ORDER}
    if not isinstance(raw_slots, dict):
        return cleaned

    for slot_name in CONCEPT_ONE_SIDED_SLOT_ORDER:
        raw_value = (raw_slots.get(slot_name, "") or "").strip()
        if not raw_value:
            continue

        tokens = []
        seen = set()
        for token in re.split(r"[/|,]", raw_value):
            canonical = _canonical_draft_hero(token)
            if not canonical:
                continue
            canonical_key = canonical.lower()
            if canonical_key in seen:
                continue
            tokens.append(canonical)
            seen.add(canonical_key)
            if len(tokens) >= 2:
                break

        cleaned[slot_name] = "/".join(tokens)

    return cleaned


def _predictor_map_values(map_entry: dict) -> dict[str, str]:
    map_values: dict[str, str] = {}
    draft = map_entry.get("draft", {})
    if not isinstance(draft, dict):
        return {field_key: "" for field_key in PREDICTOR_INPUT_ORDER}

    for group in PREDICTOR_GROUPS:
        for team_slot, slot_key, field_key in group:
            team_draft = draft.get(team_slot, {})
            if not isinstance(team_draft, dict):
                team_draft = {}
            map_values[field_key] = _canonical_draft_hero(team_draft.get(slot_key, ""))
    return map_values


def _predictor_richest_comp(map_entry: dict, team_slot: str) -> tuple[str, ...]:
    comp_sections = map_entry.get("comp", [])
    if not isinstance(comp_sections, list):
        return ()

    richest_comp: list[str] = []
    for section in comp_sections:
        if not isinstance(section, dict):
            continue
        lineup = section.get(team_slot, [])
        if not isinstance(lineup, list):
            continue

        heroes = []
        for slot in lineup:
            if not isinstance(slot, dict):
                continue
            hero_name = _canonical_draft_hero(slot.get("hero", ""))
            if hero_name:
                heroes.append(hero_name)

        if len(heroes) > len(richest_comp):
            richest_comp = heroes

    return tuple(richest_comp)


