# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def build_scrim_analytics(
    scrims: list[dict],
    *,
    perspective_label: str = "Team",
    opponent_label: str = "Opponent",
    roster_player_names: list[str] | set[str] | None = None,
) -> dict:
    ban_slot_keys = ("ban1", "ban2", "ban3", "ban4")
    ban_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    enemy_ban_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    ban_position_stats = {slot: defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0}) for slot in ban_slot_keys}
    enemy_ban_position_stats = {slot: defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0}) for slot in ban_slot_keys}
    protect_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    hero_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "unmirrored_maps": 0, "unmirrored_wins": 0, "unmirrored_losses": 0})
    map_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})
    map_draft_stats = defaultdict(
        lambda: {
            "ban_totals": 0,
            "protect_totals": 0,
            "ban_heroes": defaultdict(int),
            "protect_heroes": defaultdict(int),
        }
    )
    comp_profile_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    enemy_comp_profile_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    draft_mirror_total = 0
    draft_soft_mirror_count = 0
    draft_hard_mirror_count = 0
    comp_mirror_total = 0
    comp_soft_mirror_count = 0
    comp_hard_mirror_count = 0
    ban_next_pairs = defaultdict(lambda: defaultdict(int))
    ban_to_protect_pairs = defaultdict(lambda: defaultdict(int))
    draft_route_counts = defaultdict(int)
    draft_route_from_totals = defaultdict(int)
    lead_source_counts = {
        "ban": defaultdict(lambda: defaultdict(int)),
        "protect": defaultdict(lambda: defaultdict(int)),
    }
    lead_target_totals = {
        "ban": defaultdict(int),
        "protect": defaultdict(int),
    }
    second_order_ban_targets = defaultdict(
        lambda: {
            "ban3": defaultdict(int),
            "protect2": defaultdict(int),
            "ban4": defaultdict(int),
            "totals": {"source": 0, "ban3": 0, "protect2": 0, "ban4": 0},
        }
    )
    protect1_influence_targets = defaultdict(
        lambda: {
            "ban2": defaultdict(int),
            "ban3": defaultdict(int),
            "protect2": defaultdict(int),
            "ban4": defaultdict(int),
            "totals": {"source": 0, "ban2": 0, "ban3": 0, "protect2": 0, "ban4": 0},
        }
    )
    ban1_protect1_route_targets = defaultdict(
        lambda: {
            "ban2": defaultdict(int),
            "ban3": defaultdict(int),
            "protect2": defaultdict(int),
            "ban4": defaultdict(int),
            "totals": {"source": 0, "ban2": 0, "ban3": 0, "protect2": 0, "ban4": 0},
        }
    )
    hero_open_stats = defaultdict(
        lambda: {
            "open_maps": 0,
            "open_wins": 0,
            "open_losses": 0,
            "played_when_open": 0,
            "played_wins": 0,
            "played_losses": 0,
            "fully_open_maps": 0,
            "our_played_when_fully_open": 0,
            "enemy_played_when_fully_open": 0,
            "teammate_open_counts": defaultdict(int),
            "closed_maps": 0,
            "closed_wins": 0,
            "closed_losses": 0,
        }
    )

    total_maps = 0
    total_wins = 0
    total_losses = 0
    total_filled_bans = 0
    total_enemy_filled_bans = 0
    ban_position_totals = defaultdict(int)
    enemy_ban_position_totals = defaultdict(int)
    total_filled_protects = 0
    roster_player_keys = {
        (player_name or "").strip().lower()
        for player_name in (roster_player_names or [])
        if (player_name or "").strip()
    }

    def classify_comp_profile(heroes: list[str]) -> str:
        role_counts = defaultdict(int)
        for hero_name in heroes:
            role_name = _hero_role(hero_name)
            if role_name:
                role_counts[role_name] += 1

        strategist_count = role_counts.get("Strategist", 0) + role_counts.get("Support", 0)
        dps_count = role_counts.get("Duelist", 0) + role_counts.get("DPS", 0)
        tank_count = role_counts.get("Vanguard", 0) + role_counts.get("Tank", 0)

        if strategist_count >= 3:
            return "triple_support"
        if tank_count >= 3:
            return "triple_tank"
        if strategist_count == 2 and dps_count == 2 and tank_count == 2:
            return "two_two_two"
        return "other"

    def canonical_hero(raw_hero: str) -> str:
        hero_text = (raw_hero or "").strip()
        if not hero_text:
            return ""
        return _resolve_hero_transform_key(hero_text) or hero_text

    def draft_slot_label(slot_key: str) -> str:
        if slot_key.startswith("ban"):
            return f"Ban {slot_key[-1]}"
        if slot_key.startswith("protect"):
            return f"Protect {slot_key[-1]}"
        return slot_key

    hero_pool = {
        canonical_hero(hero_name)
        for hero_name in HEROES
        if canonical_hero(hero_name)
    }

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            total_maps += 1
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            map_outcome = get_map_outcome_for_slot(map_entry, our_team_slot)
            is_win = map_outcome == "Win"
            is_loss = map_outcome == "Loss"
            if is_win:
                total_wins += 1
            elif is_loss:
                total_losses += 1

            map_name = map_entry.get("map_name", "").strip()
            if map_name:
                map_stats[map_name]["maps"] += 1
                if is_win:
                    map_stats[map_name]["wins"] += 1
                elif is_loss:
                    map_stats[map_name]["losses"] += 1

            draft = map_entry.get("draft", {})
            our_draft = draft.get(our_team_slot, {})
            enemy_draft = draft.get(opposite_team_slot(our_team_slot), {})

            our_ban_slots = {
                slot: canonical_hero(our_draft.get(slot, ""))
                for slot in ("ban1", "ban2", "ban3", "ban4")
            }
            enemy_ban_slots = {
                slot: canonical_hero(enemy_draft.get(slot, ""))
                for slot in ("ban1", "ban2", "ban3", "ban4")
            }
            our_protect_slots = {
                slot: canonical_hero(our_draft.get(slot, ""))
                for slot in ("protect1", "protect2")
            }
            our_banned_heroes = {
                hero_name
                for hero_name in our_ban_slots.values()
                if hero_name
            }
            enemy_banned_heroes = {
                hero_name
                for hero_name in enemy_ban_slots.values()
                if hero_name
            }

            # Ban response likelihood: when we ban X in a slot, what the enemy bans
            # in their corresponding next ban slot.
            for slot in ("ban1", "ban2", "ban3", "ban4"):
                source_ban = our_ban_slots.get(slot, "")
                response_ban = enemy_ban_slots.get(slot, "")
                if source_ban and response_ban:
                    ban_next_pairs[source_ban][response_ban] += 1

            # Ban -> Protect flow based on draft phases:
            # Phase 1: ban1 leads into protect1.
            # Phase 3: protect2 happens after ban1-3, while ban4 is final.
            if our_ban_slots.get("ban1") and our_protect_slots.get("protect1"):
                ban_to_protect_pairs[our_ban_slots["ban1"]][our_protect_slots["protect1"]] += 1

                route_key = (our_ban_slots["ban1"], our_protect_slots["protect1"])
                ban1_protect1_route_targets[route_key]["totals"]["source"] += 1
                if our_ban_slots.get("ban2"):
                    ban1_protect1_route_targets[route_key]["ban2"][our_ban_slots["ban2"]] += 1
                    ban1_protect1_route_targets[route_key]["totals"]["ban2"] += 1
                if our_ban_slots.get("ban3"):
                    ban1_protect1_route_targets[route_key]["ban3"][our_ban_slots["ban3"]] += 1
                    ban1_protect1_route_targets[route_key]["totals"]["ban3"] += 1
                if our_protect_slots.get("protect2"):
                    ban1_protect1_route_targets[route_key]["protect2"][our_protect_slots["protect2"]] += 1
                    ban1_protect1_route_targets[route_key]["totals"]["protect2"] += 1
                if our_ban_slots.get("ban4"):
                    ban1_protect1_route_targets[route_key]["ban4"][our_ban_slots["ban4"]] += 1
                    ban1_protect1_route_targets[route_key]["totals"]["ban4"] += 1

            if our_protect_slots.get("protect2"):
                for slot in ("ban1", "ban2", "ban3"):
                    if our_ban_slots.get(slot):
                        ban_to_protect_pairs[our_ban_slots[slot]][our_protect_slots["protect2"]] += 1

            ban1_hero = our_ban_slots.get("ban1", "")
            ban2_hero = our_ban_slots.get("ban2", "")
            if ban1_hero and ban2_hero:
                second_order_ban_targets[(ban1_hero, ban2_hero)]["totals"]["source"] += 1
                ban3_hero = our_ban_slots.get("ban3", "")
                protect2_hero = our_protect_slots.get("protect2", "")
                ban4_hero = our_ban_slots.get("ban4", "")
                if ban3_hero:
                    second_order_ban_targets[(ban1_hero, ban2_hero)]["ban3"][ban3_hero] += 1
                    second_order_ban_targets[(ban1_hero, ban2_hero)]["totals"]["ban3"] += 1
                if protect2_hero:
                    second_order_ban_targets[(ban1_hero, ban2_hero)]["protect2"][protect2_hero] += 1
                    second_order_ban_targets[(ban1_hero, ban2_hero)]["totals"]["protect2"] += 1
                if ban4_hero:
                    second_order_ban_targets[(ban1_hero, ban2_hero)]["ban4"][ban4_hero] += 1
                    second_order_ban_targets[(ban1_hero, ban2_hero)]["totals"]["ban4"] += 1

            protect1_hero = our_protect_slots.get("protect1", "")
            if protect1_hero:
                protect1_influence_targets[protect1_hero]["totals"]["source"] += 1
                if our_ban_slots.get("ban2"):
                    protect1_influence_targets[protect1_hero]["ban2"][our_ban_slots["ban2"]] += 1
                    protect1_influence_targets[protect1_hero]["totals"]["ban2"] += 1
                if our_ban_slots.get("ban3"):
                    protect1_influence_targets[protect1_hero]["ban3"][our_ban_slots["ban3"]] += 1
                    protect1_influence_targets[protect1_hero]["totals"]["ban3"] += 1
                if our_protect_slots.get("protect2"):
                    protect1_influence_targets[protect1_hero]["protect2"][our_protect_slots["protect2"]] += 1
                    protect1_influence_targets[protect1_hero]["totals"]["protect2"] += 1
                if our_ban_slots.get("ban4"):
                    protect1_influence_targets[protect1_hero]["ban4"][our_ban_slots["ban4"]] += 1
                    protect1_influence_targets[protect1_hero]["totals"]["ban4"] += 1

            draft_sequence = [
                (slot, canonical_hero(our_draft.get(slot, "")))
                for slot in ("ban1", "protect1", "ban2", "ban3", "protect2", "ban4")
                if canonical_hero(our_draft.get(slot, ""))
            ]

            for idx in range(len(draft_sequence) - 1):
                from_slot, from_hero = draft_sequence[idx]
                to_slot, to_hero = draft_sequence[idx + 1]
                draft_route_counts[(from_slot, from_hero, to_slot, to_hero)] += 1
                draft_route_from_totals[(from_slot, from_hero)] += 1

            for idx in range(1, len(draft_sequence)):
                target_slot, target_hero = draft_sequence[idx]
                target_type = "ban" if target_slot.startswith("ban") else "protect"
                lead_target_totals[target_type][target_hero] += 1
                for prev_slot, prev_hero in draft_sequence[:idx]:
                    source_key = f"{draft_slot_label(prev_slot)}|{prev_hero}"
                    lead_source_counts[target_type][target_hero][source_key] += 1

            our_draft_heroes = [
                (_resolve_hero_transform_key((hero or "").strip()) or (hero or "").strip())
                for hero in our_draft.values()
                if (hero or "").strip()
            ]
            enemy_draft_heroes = [
                (_resolve_hero_transform_key((hero or "").strip()) or (hero or "").strip())
                for hero in enemy_draft.values()
                if (hero or "").strip()
            ]
            if our_draft_heroes and enemy_draft_heroes:
                shared_draft_heroes = len(set(our_draft_heroes) & set(enemy_draft_heroes))
                draft_mirror_total += 1
                if shared_draft_heroes >= 4:
                    draft_soft_mirror_count += 1

            for slot_key, hero in our_draft.items():
                hero_name = (hero or "").strip()
                if not hero_name:
                    continue

                if "ban" in slot_key:
                    total_filled_bans += 1
                    ban_stats[hero_name]["count"] += 1
                    if map_name:
                        map_draft_stats[map_name]["ban_totals"] += 1
                        map_draft_stats[map_name]["ban_heroes"][hero_name] += 1
                    if slot_key in ban_position_stats:
                        ban_position_totals[slot_key] += 1
                        ban_position_stats[slot_key][hero_name]["count"] += 1
                    if is_win:
                        ban_stats[hero_name]["wins"] += 1
                        if slot_key in ban_position_stats:
                            ban_position_stats[slot_key][hero_name]["wins"] += 1
                    elif is_loss:
                        ban_stats[hero_name]["losses"] += 1
                        if slot_key in ban_position_stats:
                            ban_position_stats[slot_key][hero_name]["losses"] += 1
                elif "protect" in slot_key:
                    total_filled_protects += 1
                    protect_stats[hero_name]["count"] += 1
                    if map_name:
                        map_draft_stats[map_name]["protect_totals"] += 1
                        map_draft_stats[map_name]["protect_heroes"][hero_name] += 1
                    if is_win:
                        protect_stats[hero_name]["wins"] += 1
                    elif is_loss:
                        protect_stats[hero_name]["losses"] += 1

            for slot_key, hero in enemy_draft.items():
                hero_name = (hero or "").strip()
                if not hero_name or "ban" not in slot_key:
                    continue

                total_enemy_filled_bans += 1
                enemy_ban_stats[hero_name]["count"] += 1
                if slot_key in enemy_ban_position_stats:
                    enemy_ban_position_totals[slot_key] += 1
                    enemy_ban_position_stats[slot_key][hero_name]["count"] += 1
                if is_win:
                    enemy_ban_stats[hero_name]["wins"] += 1
                    if slot_key in enemy_ban_position_stats:
                        enemy_ban_position_stats[slot_key][hero_name]["wins"] += 1
                elif is_loss:
                    enemy_ban_stats[hero_name]["losses"] += 1
                    if slot_key in enemy_ban_position_stats:
                        enemy_ban_position_stats[slot_key][hero_name]["losses"] += 1

            hero_instances_in_map: list[str] = []
            comp_profiles_in_map = set()
            enemy_comp_profiles_in_map = set()
            for section in map_entry.get("comp", []):
                hero_instances_in_map.extend(_canonical_section_hero_instances(section, our_team_slot))

                section_heroes = _canonical_section_hero_instances(section, our_team_slot)
                if section_heroes:
                    comp_profiles_in_map.add(classify_comp_profile(section_heroes))

                enemy_section_heroes = _canonical_section_hero_instances(section, opposite_team_slot(our_team_slot))
                if enemy_section_heroes:
                    enemy_comp_profiles_in_map.add(classify_comp_profile(enemy_section_heroes))

                if section_heroes and enemy_section_heroes:
                    shared_comp_heroes = len(set(section_heroes) & set(enemy_section_heroes))
                    comp_mirror_total += 1
                    if shared_comp_heroes >= 4:
                        comp_soft_mirror_count += 1

            canonical_heroes_in_map = {
                canonical_hero(hero_name)
                for hero_name in hero_instances_in_map
                if canonical_hero(hero_name)
            }
            enemy_hero_instances_in_map = _canonical_map_hero_instances(map_entry, opposite_team_slot(our_team_slot))
            enemy_canonical_heroes_in_map = {
                canonical_hero(hero_name)
                for hero_name in enemy_hero_instances_in_map
                if canonical_hero(hero_name)
            }
            our_hero_players_in_map = defaultdict(set)
            for section in map_entry.get("comp", []):
                for slot in section.get(our_team_slot, []):
                    if not isinstance(slot, dict):
                        continue
                    hero_name = canonical_hero(slot.get("hero", ""))
                    player_name = (slot.get("player", "") or "").strip()
                    player_key = player_name.lower()
                    if hero_name and player_name and (
                        not roster_player_keys or player_key in roster_player_keys
                    ):
                        our_hero_players_in_map[hero_name].add(player_name)

            # Only track open/closed stats on maps where the draft was actually logged.
            # Without ban data, every hero in hero_pool looks "open" (empty banned set),
            # which inflates open_maps for all heroes on draft-less maps.
            has_draft_data = (
                any(our_ban_slots.values())
                or any(enemy_ban_slots.values())
                or any(our_protect_slots.values())
            )
            if not has_draft_data:
                # No draft was recorded for this map; skip open/closed tracking.
                pass
            else:
                tracked_heroes = hero_pool | enemy_banned_heroes | canonical_heroes_in_map
                for hero_name in tracked_heroes:
                    is_open = hero_name not in enemy_banned_heroes
                    is_played = hero_name in canonical_heroes_in_map
                    if is_open:
                        hero_open_stats[hero_name]["open_maps"] += 1
                        if is_win:
                            hero_open_stats[hero_name]["open_wins"] += 1
                        elif is_loss:
                            hero_open_stats[hero_name]["open_losses"] += 1

                        if is_played:
                            hero_open_stats[hero_name]["played_when_open"] += 1
                            if is_win:
                                hero_open_stats[hero_name]["played_wins"] += 1
                            elif is_loss:
                                hero_open_stats[hero_name]["played_losses"] += 1
                            for player_name in our_hero_players_in_map.get(hero_name, []):
                                hero_open_stats[hero_name]["teammate_open_counts"][player_name] += 1

                    is_fully_open = hero_name not in enemy_banned_heroes and hero_name not in our_banned_heroes
                    if is_fully_open:
                        hero_open_stats[hero_name]["fully_open_maps"] += 1
                        if hero_name in canonical_heroes_in_map:
                            hero_open_stats[hero_name]["our_played_when_fully_open"] += 1
                        if hero_name in enemy_canonical_heroes_in_map:
                            hero_open_stats[hero_name]["enemy_played_when_fully_open"] += 1

                    # "Banned" means the enemy specifically banned the hero (not us banning it).
                    # Tracked separately from is_fully_open so a hero we ban doesn't pollute
                    # the "WR When Banned" win-rate or inflate open_maps vs closed_maps totals.
                    if not is_open:
                        hero_open_stats[hero_name]["closed_maps"] += 1
                        if is_win:
                            hero_open_stats[hero_name]["closed_wins"] += 1
                        elif is_loss:
                            hero_open_stats[hero_name]["closed_losses"] += 1

            # Determine if draft is unmirrored (1-2 shared heroes)
            is_draft_unmirrored = False
            if our_draft_heroes and enemy_draft_heroes:
                shared_draft_heroes = len(set(our_draft_heroes) & set(enemy_draft_heroes))
                is_draft_unmirrored = 1 <= shared_draft_heroes <= 2

            for hero_name in hero_instances_in_map:
                hero_stats[hero_name]["maps"] += 1
                if is_win:
                    hero_stats[hero_name]["wins"] += 1
                elif is_loss:
                    hero_stats[hero_name]["losses"] += 1
                
                if is_draft_unmirrored:
                    hero_stats[hero_name]["unmirrored_maps"] += 1
                    if is_win:
                        hero_stats[hero_name]["unmirrored_wins"] += 1
                    elif is_loss:
                        hero_stats[hero_name]["unmirrored_losses"] += 1

            for profile_key in comp_profiles_in_map:
                comp_profile_stats[profile_key]["count"] += 1
                if is_win:
                    comp_profile_stats[profile_key]["wins"] += 1
                elif is_loss:
                    comp_profile_stats[profile_key]["losses"] += 1

            for profile_key in enemy_comp_profiles_in_map:
                enemy_comp_profile_stats[profile_key]["count"] += 1
                if is_win:
                    enemy_comp_profile_stats[profile_key]["wins"] += 1
                elif is_loss:
                    enemy_comp_profile_stats[profile_key]["losses"] += 1

    def pct(part: int, whole: int) -> float:
        return round((part / whole) * 100, 1) if whole else 0.0

    ban_rows = []
    for hero, stats in ban_stats.items():
        ban_rows.append(
            {
                "hero": hero,
                "count": stats["count"],
                "ban_rate": pct(stats["count"], total_filled_bans),
            }
        )
    ban_rows.sort(key=lambda r: r["count"], reverse=True)

    enemy_ban_rows = []
    for hero, stats in enemy_ban_stats.items():
        enemy_ban_rows.append(
            {
                "hero": hero,
                "count": stats["count"],
                "ban_rate": pct(stats["count"], total_enemy_filled_bans),
            }
        )
    enemy_ban_rows.sort(key=lambda r: r["count"], reverse=True)

    ban_next_rows = []
    for source_hero, response_counts in ban_next_pairs.items():
        total_sequences = sum(response_counts.values())
        response_rows = sorted(response_counts.items(), key=lambda item: item[1], reverse=True)
        top_response, top_count = response_rows[0] if response_rows else ("", 0)
        ban_next_rows.append(
            {
                "ban_hero": source_hero,
                "total": total_sequences,
                "top_enemy_ban": top_response,
                "top_count": top_count,
                "top_rate": pct(top_count, total_sequences),
                "responses": [
                    {
                        "hero": response_hero,
                        "count": response_count,
                        "rate": pct(response_count, total_sequences),
                    }
                    for response_hero, response_count in response_rows[:3]
                ],
            }
        )
    ban_next_rows.sort(key=lambda row: (row["total"], row["top_rate"]), reverse=True)

    ban_to_protect_rows = []
    for source_hero, protect_counts in ban_to_protect_pairs.items():
        total_links = sum(protect_counts.values())
        protect_rows = sorted(protect_counts.items(), key=lambda item: item[1], reverse=True)
        top_protect, top_count = protect_rows[0] if protect_rows else ("", 0)
        ban_to_protect_rows.append(
            {
                "ban_hero": source_hero,
                "total": total_links,
                "top_protect": top_protect,
                "top_count": top_count,
                "top_rate": pct(top_count, total_links),
                "protects": [
                    {
                        "hero": protect_hero,
                        "count": protect_count,
                        "rate": pct(protect_count, total_links),
                    }
                    for protect_hero, protect_count in protect_rows[:3]
                ],
            }
        )
    ban_to_protect_rows.sort(key=lambda row: (row["total"], row["top_rate"]), reverse=True)

    draft_route_rows = []
    for (from_slot, from_hero, to_slot, to_hero), count in draft_route_counts.items():
        total_from = draft_route_from_totals[(from_slot, from_hero)]
        draft_route_rows.append(
            {
                "from_slot": draft_slot_label(from_slot),
                "from_hero": from_hero,
                "to_slot": draft_slot_label(to_slot),
                "to_hero": to_hero,
                "count": count,
                "rate": pct(count, total_from),
            }
        )
    draft_route_rows.sort(key=lambda row: (row["count"], row["rate"]), reverse=True)

    def build_lead_rows(target_type: str) -> list[dict]:
        rows = []
        for target_hero, source_counts in lead_source_counts[target_type].items():
            total = lead_target_totals[target_type][target_hero]
            sorted_sources = sorted(source_counts.items(), key=lambda item: item[1], reverse=True)
            if not sorted_sources:
                continue
            top_source_key, top_count = sorted_sources[0]
            top_slot, top_hero = top_source_key.split("|", 1)
            rows.append(
                {
                    "target_hero": target_hero,
                    "total": total,
                    "top_source_slot": top_slot,
                    "top_source_hero": top_hero,
                    "top_count": top_count,
                    "top_rate": pct(top_count, total),
                }
            )
        rows.sort(key=lambda row: (row["total"], row["top_rate"]), reverse=True)
        return rows

    lead_to_ban_rows = build_lead_rows("ban")
    lead_to_protect_rows = build_lead_rows("protect")

    total_second_order_sources = sum(
        target_data["totals"].get("source", 0)
        for target_data in second_order_ban_targets.values()
    )

    second_order_ban_rows = []
    for (ban1_hero, ban2_hero), target_data in second_order_ban_targets.items():
        source_total = target_data["totals"].get("source", 0)
        ban3_total = target_data["totals"]["ban3"]
        protect2_total = target_data["totals"]["protect2"]
        ban4_total = target_data["totals"]["ban4"]

        ban3_sorted = sorted(target_data["ban3"].items(), key=lambda item: item[1], reverse=True)
        protect2_sorted = sorted(target_data["protect2"].items(), key=lambda item: item[1], reverse=True)
        ban4_sorted = sorted(target_data["ban4"].items(), key=lambda item: item[1], reverse=True)

        top_ban3, top_ban3_count = ban3_sorted[0] if ban3_sorted else ("", 0)
        top_protect2, top_protect2_count = protect2_sorted[0] if protect2_sorted else ("", 0)
        top_ban4, top_ban4_count = ban4_sorted[0] if ban4_sorted else ("", 0)

        second_order_ban_rows.append(
            {
                "ban1_hero": ban1_hero,
                "ban1_rate": pct(source_total, total_second_order_sources),
                "ban2_hero": ban2_hero,
                "ban2_rate": pct(source_total, total_second_order_sources),
                "source_total": source_total,
                "ban3_hero": top_ban3,
                "ban3_count": top_ban3_count,
                "ban3_rate": pct(top_ban3_count, ban3_total),
                "ban3_total": ban3_total,
                "protect2_hero": top_protect2,
                "protect2_count": top_protect2_count,
                "protect2_rate": pct(top_protect2_count, protect2_total),
                "protect2_total": protect2_total,
                "ban4_hero": top_ban4,
                "ban4_count": top_ban4_count,
                "ban4_rate": pct(top_ban4_count, ban4_total),
                "ban4_total": ban4_total,
                "sample_total": ban3_total + protect2_total + ban4_total,
            }
        )
    second_order_ban_rows.sort(key=lambda row: row["sample_total"], reverse=True)

    def top_slot_pick(slot_counts: dict, total: int) -> dict:
        sorted_rows = sorted(slot_counts.items(), key=lambda item: item[1], reverse=True)
        hero, count = sorted_rows[0] if sorted_rows else ("", 0)
        return {
            "hero": hero,
            "count": count,
            "total": total,
            "rate": pct(count, total),
        }

    total_protect1_sources = sum(
        target_data["totals"].get("source", 0)
        for target_data in protect1_influence_targets.values()
    )

    protect1_influence_rows = []
    for protect1_hero, target_data in protect1_influence_targets.items():
        source_total = target_data["totals"].get("source", 0)
        ban2_top = top_slot_pick(target_data["ban2"], target_data["totals"]["ban2"])
        ban3_top = top_slot_pick(target_data["ban3"], target_data["totals"]["ban3"])
        protect2_top = top_slot_pick(target_data["protect2"], target_data["totals"]["protect2"])
        ban4_top = top_slot_pick(target_data["ban4"], target_data["totals"]["ban4"])
        protect1_influence_rows.append(
            {
                "protect1_hero": protect1_hero,
                "protect1_rate": pct(source_total, total_protect1_sources),
                "source_total": source_total,
                "ban2": ban2_top,
                "ban3": ban3_top,
                "protect2": protect2_top,
                "ban4": ban4_top,
                "sample_total": sum(target_data["totals"].values()),
            }
        )
    protect1_influence_rows.sort(key=lambda row: row["sample_total"], reverse=True)

    total_ban1_protect1_sources = sum(
        target_data["totals"].get("source", 0)
        for target_data in ban1_protect1_route_targets.values()
    )

    most_likely_ban_route_rows = []
    for (ban1_hero, protect1_hero), target_data in ban1_protect1_route_targets.items():
        source_total = target_data["totals"].get("source", 0)
        next_nodes = []
        for slot_key, slot_label in (("ban2", "Ban 2"), ("ban3", "Ban 3"), ("protect2", "P2"), ("ban4", "Ban 4")):
            slot_total = target_data["totals"].get(slot_key, 0)
            sorted_rows = sorted(target_data[slot_key].items(), key=lambda item: item[1], reverse=True)
            hero_name, hero_count = sorted_rows[0] if sorted_rows else ("", 0)
            if hero_name:
                next_nodes.append({"hero": hero_name, "label": slot_label, "rate": pct(hero_count, slot_total)})

        if next_nodes:
            source_rate = pct(source_total, total_ban1_protect1_sources)
            most_likely_ban_route_rows.append(
                {
                    "source_nodes": [
                        {"hero": ban1_hero, "label": "Ban 1", "rate": source_rate},
                        {"hero": protect1_hero, "label": "Protect 1", "rate": source_rate},
                    ],
                    "next_nodes": next_nodes,
                    "source_total": source_total,
                    "top_rate": max(node["rate"] for node in next_nodes),
                }
            )

    most_likely_ban_route_rows.sort(key=lambda row: (row["source_total"], row["top_rate"]), reverse=True)

    overall_win_rate = pct(total_wins, total_maps)

    ban_protect_rows = []
    all_draft_heroes = set(ban_stats.keys()) | set(protect_stats.keys())
    for hero in all_draft_heroes:
        ban_count = ban_stats[hero]["count"]
        protect_count = protect_stats[hero]["count"]
        ban_rate = pct(ban_count, total_filled_bans)
        protect_rate = pct(protect_count, total_filled_protects)
        ban_win_rate = pct(ban_stats[hero]["wins"], ban_count)
        protect_win_rate = pct(protect_stats[hero]["wins"], protect_count)
        ban_delta = round(ban_win_rate - overall_win_rate, 1) if ban_count else 0.0
        protect_delta = round(protect_win_rate - overall_win_rate, 1) if protect_count else 0.0
        winrate_gap = round(protect_win_rate - ban_win_rate, 1) if ban_count and protect_count else None
        rate_gap = round(ban_rate - protect_rate, 1)
        draft_presence = round(ban_rate + protect_rate, 1)
        if rate_gap >= 5:
            leaning = "Ban leaning"
        elif rate_gap <= -5:
            leaning = "Protect leaning"
        else:
            leaning = "Balanced"

        ban_protect_rows.append(
            {
                "hero": hero,
                "ban_count": ban_count,
                "protect_count": protect_count,
                "ban_rate": ban_rate,
                "protect_rate": protect_rate,
                "ban_win_rate": ban_win_rate,
                "protect_win_rate": protect_win_rate,
                "ban_delta": ban_delta,
                "protect_delta": protect_delta,
                "winrate_gap": winrate_gap,
                "rate_gap": rate_gap,
                "draft_presence": draft_presence,
                "leaning": leaning,
            }
        )
    ban_protect_rows.sort(key=lambda r: (r["draft_presence"], abs(r["rate_gap"]), r["ban_count"] + r["protect_count"]), reverse=True)

    def calc_correlation(pairs: list[tuple[int, int]]) -> float | None:
        if len(pairs) < 2:
            return None
        xs = [pair[0] for pair in pairs]
        ys = [pair[1] for pair in pairs]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        num = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
        den_x = sum((x - mean_x) ** 2 for x in xs)
        den_y = sum((y - mean_y) ** 2 for y in ys)
        if den_x <= 0 or den_y <= 0:
            return None
        return round(num / ((den_x ** 0.5) * (den_y ** 0.5)), 2)

    ban_protect_correlation = calc_correlation(
        [(row["ban_count"], row["protect_count"]) for row in ban_protect_rows if row["ban_count"] or row["protect_count"]]
    )
    top_ban_protect = ban_protect_rows[0] if ban_protect_rows else None

    ban_diff_rows = []
    for hero in set(ban_stats.keys()) | set(enemy_ban_stats.keys()):
        our_count = ban_stats[hero]["count"]
        enemy_count = enemy_ban_stats[hero]["count"]
        our_rate = pct(our_count, total_filled_bans)
        enemy_rate = pct(enemy_count, total_enemy_filled_bans)
        rate_diff = round(our_rate - enemy_rate, 1)
        if rate_diff > 0:
            edge_label = "We ban more"
        elif rate_diff < 0:
            edge_label = "Enemy bans more"
        else:
            edge_label = "Even"

        ban_diff_rows.append(
            {
                "hero": hero,
                "our_count": our_count,
                "enemy_count": enemy_count,
                "our_rate": our_rate,
                "enemy_rate": enemy_rate,
                "rate_diff": rate_diff,
                "abs_diff": abs(rate_diff),
                "edge_label": edge_label,
            }
        )
    ban_diff_rows.sort(key=lambda r: (r["abs_diff"], r["our_count"] + r["enemy_count"]), reverse=True)

    def build_ban_position_rows(position_stats: dict, position_totals: dict) -> list[dict]:
        rows = []
        for slot_key in ban_slot_keys:
            total_for_slot = position_totals.get(slot_key, 0)
            hero_rows = []
            for hero, stats in position_stats[slot_key].items():
                hero_rows.append(
                    {
                        "hero": hero,
                        "count": stats["count"],
                        "rate": pct(stats["count"], total_for_slot),
                        "win_rate": pct(stats["wins"], stats["count"]),
                    }
                )
            hero_rows.sort(key=lambda r: (r["count"], r["rate"], r["win_rate"]), reverse=True)
            top_row = hero_rows[0] if hero_rows else None
            rows.append(
                {
                    "slot_key": slot_key,
                    "slot_label": f"Ban {slot_key[-1]}",
                    "total": total_for_slot,
                    "unique_heroes": len(hero_rows),
                    "top_hero": top_row["hero"] if top_row else "-",
                    "top_count": top_row["count"] if top_row else 0,
                    "top_rate": top_row["rate"] if top_row else 0,
                    "hero_rows": hero_rows[:3],
                }
            )
        return rows

    ban_position_rows = build_ban_position_rows(ban_position_stats, ban_position_totals)
    enemy_ban_position_rows = build_ban_position_rows(enemy_ban_position_stats, enemy_ban_position_totals)

    def add_ban_position_insights(primary_rows: list[dict], secondary_rows: list[dict]) -> None:
        secondary_lookup = {row["slot_key"]: row for row in secondary_rows}
        for row in primary_rows:
            top_hero = row.get("top_hero") or ""
            if not top_hero or top_hero == "-":
                row["insight_state"] = "even"
                row["insight_rate"] = 0
                row["insight_compare_rate"] = 0
                row["insight_hero"] = ""
                continue

            other_row = secondary_lookup.get(row["slot_key"], {})
            other_hero_rows = other_row.get("hero_rows", [])
            other_match = next((hero_row for hero_row in other_hero_rows if hero_row["hero"] == top_hero), None)
            other_rate = other_match["rate"] if other_match else 0
            rate = row.get("top_rate", 0)
            diff = round(rate - other_rate, 1)
            if diff > 0:
                state = "more"
            elif diff < 0:
                state = "less"
            else:
                state = "even"

            row["insight_state"] = state
            row["insight_rate"] = rate
            row["insight_compare_rate"] = other_rate
            row["insight_diff"] = diff
            row["insight_hero"] = top_hero

    add_ban_position_insights(ban_position_rows, enemy_ban_position_rows)
    add_ban_position_insights(enemy_ban_position_rows, ban_position_rows)

    def build_ban_phase_variation_summary(position_rows: list[dict], side_label: str) -> dict:
        early_rows = [row for row in position_rows if row.get("slot_key") in ("ban1", "ban2")]
        late_rows = [row for row in position_rows if row.get("slot_key") in ("ban3", "ban4")]

        if not early_rows or not late_rows:
            return {
                "side_label": side_label,
                "early_unique_avg": 0.0,
                "late_unique_avg": 0.0,
                "early_top_rate_avg": 0.0,
                "late_top_rate_avg": 0.0,
                "variation_unique_diff": 0.0,
                "variation_top_rate_diff": 0.0,
                "signal": "insufficient",
                "message": "Not enough ban slot data yet to evaluate Ban 1-2 vs Ban 3-4 variation.",
            }

        early_unique_avg = round(sum(row.get("unique_heroes", 0) for row in early_rows) / len(early_rows), 1)
        late_unique_avg = round(sum(row.get("unique_heroes", 0) for row in late_rows) / len(late_rows), 1)
        early_top_rate_avg = round(sum(row.get("top_rate", 0) for row in early_rows) / len(early_rows), 1)
        late_top_rate_avg = round(sum(row.get("top_rate", 0) for row in late_rows) / len(late_rows), 1)

        variation_unique_diff = round(late_unique_avg - early_unique_avg, 1)
        variation_top_rate_diff = round(early_top_rate_avg - late_top_rate_avg, 1)

        strong_variation = variation_unique_diff >= 0.5 or variation_top_rate_diff >= 8
        mild_variation = variation_unique_diff > 0 or variation_top_rate_diff > 0

        if strong_variation:
            signal = "strong"
            message = (
                f"{side_label} shows strong Ban 3-4 variation versus Ban 1-2, "
                "which often indicates team-specific targeting."
            )
        elif mild_variation:
            signal = "moderate"
            message = (
                f"{side_label} shows some extra variation in Ban 3-4 compared to Ban 1-2, "
                "suggesting partial team-specific adjustments."
            )
        else:
            signal = "low"
            message = (
                f"{side_label} has similar variation across Ban 1-2 and Ban 3-4, "
                "so bans currently look more meta-stable."
            )

        return {
            "side_label": side_label,
            "early_unique_avg": early_unique_avg,
            "late_unique_avg": late_unique_avg,
            "early_top_rate_avg": early_top_rate_avg,
            "late_top_rate_avg": late_top_rate_avg,
            "variation_unique_diff": variation_unique_diff,
            "variation_top_rate_diff": variation_top_rate_diff,
            "signal": signal,
            "message": message,
        }

    main_ban_variation = build_ban_phase_variation_summary(ban_position_rows, perspective_label)
    enemy_ban_variation = build_ban_phase_variation_summary(enemy_ban_position_rows, opponent_label)

    protect_rows = []
    for hero, stats in protect_stats.items():
        protect_rows.append(
            {
                "hero": hero,
                "count": stats["count"],
                "protect_rate": pct(stats["count"], total_filled_protects),
                "win_rate": pct(stats["wins"], stats["count"]),
            }
        )
    protect_rows.sort(key=lambda r: (r["count"], r["win_rate"]), reverse=True)

    hero_rows = []
    for hero, stats in hero_stats.items():
        hero_rows.append(
            {
                "hero": hero,
                "maps": stats["maps"],
                "win_rate": pct(stats["wins"], stats["maps"]),
                "unmirrored_maps": stats["unmirrored_maps"],
                "unmirrored_win_rate": pct(stats["unmirrored_wins"], stats["unmirrored_maps"]),
            }
        )
    hero_rows.sort(key=lambda r: (r["maps"], r["win_rate"]), reverse=True)

    map_rows = []
    for map_name, stats in map_stats.items():
        map_rows.append(
            {
                "map_name": map_name,
                "maps": stats["maps"],
                "win_rate": pct(stats["wins"], stats["maps"]),
            }
        )
    map_rows.sort(key=lambda r: (r["maps"], r["win_rate"]), reverse=True)

    # If one hero is clearly the overall most banned hero, suppress it in the
    # per-map spotlight so map-specific trends stay visible.
    dominant_ban_hero = ""
    if ban_stats:
        sorted_global_bans = sorted(
            ((hero, stats["count"]) for hero, stats in ban_stats.items()),
            key=lambda item: item[1],
            reverse=True,
        )
        if sorted_global_bans:
            top_count = sorted_global_bans[0][1]
            top_heroes = [hero for hero, count in sorted_global_bans if count == top_count]
            if len(top_heroes) == 1:
                dominant_ban_hero = top_heroes[0]

    map_draft_rows = []
    for map_name, stats in map_draft_stats.items():
        ban_totals = stats["ban_totals"]
        protect_totals = stats["protect_totals"]

        top_ban_hero = ""
        top_ban_count = 0
        if stats["ban_heroes"]:
            sorted_map_bans = sorted(
                stats["ban_heroes"].items(),
                key=lambda item: item[1],
                reverse=True,
            )
            top_ban_hero, top_ban_count = sorted_map_bans[0]
            if dominant_ban_hero and top_ban_hero == dominant_ban_hero and len(sorted_map_bans) > 1:
                top_ban_hero, top_ban_count = sorted_map_bans[1]

        top_protect_hero = ""
        top_protect_count = 0
        if stats["protect_heroes"]:
            top_protect_hero, top_protect_count = max(stats["protect_heroes"].items(), key=lambda item: item[1])

        top_ban_rate = pct(top_ban_count, ban_totals)
        top_protect_rate = pct(top_protect_count, protect_totals)
        is_skip = top_ban_hero and top_ban_count >= 2 and top_ban_rate >= 35
        if is_skip:
            recommendation = "Skip: banned too often"
        elif top_ban_hero:
            recommendation = "Playable: bans not overwhelming"
        else:
            recommendation = "No draft data"

        map_draft_rows.append(
            {
                "map_name": map_name,
                "map_image": get_map_image_url(map_name),
                "top_ban_hero": top_ban_hero,
                "top_ban_count": top_ban_count,
                "top_ban_rate": top_ban_rate,
                "top_protect_hero": top_protect_hero,
                "top_protect_count": top_protect_count,
                "top_protect_rate": top_protect_rate,
                "recommendation": recommendation,
                "skip_flag": is_skip,
            }
        )
    map_draft_rows.sort(key=lambda r: (r["skip_flag"], r["top_ban_rate"], r["top_ban_count"]), reverse=True)

    hero_open_rows = []
    for hero_name, stats in hero_open_stats.items():
        open_maps = stats["open_maps"]
        played_when_open = stats["played_when_open"]
        if not open_maps or not played_when_open:
            continue

        not_played_when_open = max(0, open_maps - played_when_open)
        closed_maps = stats["closed_maps"]
        win_rate_when_open = pct(stats["open_wins"], open_maps)
        win_rate_when_open_played = pct(stats["played_wins"], played_when_open)
        open_not_played_wins = max(0, stats["open_wins"] - stats["played_wins"])
        win_rate_when_open_not_played = pct(open_not_played_wins, not_played_when_open)
        win_rate_when_closed = pct(stats["closed_wins"], closed_maps)
        open_vs_closed_delta = round(win_rate_when_open - win_rate_when_closed, 1) if closed_maps else None
        played_vs_not_played_open_delta = (
            round(win_rate_when_open_played - win_rate_when_open_not_played, 1)
            if played_when_open and not_played_when_open
            else None
        )
        open_vs_overall_delta = round(win_rate_when_open - overall_win_rate, 1)
        play_when_open_rate = pct(played_when_open, open_maps)
        total_observed_maps = open_maps + closed_maps
        ban_rate = pct(closed_maps, total_observed_maps)
        fully_open_maps = stats["fully_open_maps"]
        our_played_when_fully_open = stats["our_played_when_fully_open"]
        enemy_played_when_fully_open = stats["enemy_played_when_fully_open"]
        our_fully_open_rate = pct(our_played_when_fully_open, fully_open_maps)
        enemy_fully_open_rate = pct(enemy_played_when_fully_open, fully_open_maps)
        fully_open_play_diff = round(our_fully_open_rate - enemy_fully_open_rate, 1) if fully_open_maps else None
        if fully_open_play_diff is None:
            fully_open_edge_label = "--"
        elif fully_open_play_diff > 0:
            fully_open_edge_label = "We play more"
        elif fully_open_play_diff < 0:
            fully_open_edge_label = "Opponent plays more"
        else:
            fully_open_edge_label = "Even"
        teammate_open_counts = stats["teammate_open_counts"]
        top_teammate_name = ""
        top_teammate_count = 0
        if teammate_open_counts:
            top_teammate_name, top_teammate_count = max(
                teammate_open_counts.items(),
                key=lambda item: (item[1], item[0].lower()),
            )
        top_teammate_rate = pct(top_teammate_count, played_when_open)

        hero_open_rows.append(
            {
                "hero": _resolve_hero_transform_key(hero_name) or hero_name,
                "open_maps": open_maps,
                "open_rate": pct(open_maps, total_maps),
                "banned_maps": closed_maps,
                "played_when_open": played_when_open,
                "not_played_when_open": not_played_when_open,
                "play_when_open_rate": play_when_open_rate,
                "win_rate_when_open": win_rate_when_open,
                "win_rate_when_open_played": win_rate_when_open_played,
                "win_rate_when_open_not_played": win_rate_when_open_not_played,
                "played_vs_not_played_open_delta": played_vs_not_played_open_delta,
                "win_rate_when_closed": win_rate_when_closed,
                "win_rate_when_banned": win_rate_when_closed,
                "open_vs_closed_delta": open_vs_closed_delta,
                "open_vs_banned_delta": open_vs_closed_delta,
                "open_vs_overall_delta": open_vs_overall_delta,
                "fully_open_maps": fully_open_maps,
                "our_played_when_fully_open": our_played_when_fully_open,
                "enemy_played_when_fully_open": enemy_played_when_fully_open,
                "ban_rate": ban_rate,
                "our_fully_open_rate": our_fully_open_rate,
                "enemy_fully_open_rate": enemy_fully_open_rate,
                "fully_open_play_diff": fully_open_play_diff,
                "fully_open_edge_label": fully_open_edge_label,
                "top_teammate_name": top_teammate_name,
                "top_teammate_count": top_teammate_count,
                "top_teammate_rate": top_teammate_rate,
            }
        )
    def _open_priority(row: dict) -> tuple:
        # Primary: composite of play-rate x win-rate-when-played (both 0-100),
        # scaled so a hero always played and always winning scores 10000.
        # This surfaces "must-play and winning" heroes regardless of sample size.
        play_rate = row["play_when_open_rate"]          # 0-100
        wr_played = row["win_rate_when_open_played"]    # 0-100
        composite = play_rate * wr_played               # max 10000
        # Secondary: sample confidence (more open maps = more reliable signal)
        return (composite, row["played_when_open"], row["open_maps"])

    hero_open_rows.sort(key=_open_priority, reverse=True)

    mirror_rates = {
        "draft": {
            "samples": draft_mirror_total,
            "mirror_count": draft_soft_mirror_count,
            "mirror_rate": pct(draft_soft_mirror_count, draft_mirror_total),
        },
        "comp": {
            "samples": comp_mirror_total,
            "mirror_count": comp_soft_mirror_count,
            "mirror_rate": pct(comp_soft_mirror_count, comp_mirror_total),
        },
    }

    triple_support_count = comp_profile_stats["triple_support"]["count"]
    two_two_two_count = comp_profile_stats["two_two_two"]["count"]
    triple_tank_count = comp_profile_stats["triple_tank"]["count"]
    triple_support_rate = pct(triple_support_count, total_maps)
    two_two_two_rate = pct(two_two_two_count, total_maps)
    triple_tank_rate = pct(triple_tank_count, total_maps)
    comp_difference_rate = round(triple_support_rate - two_two_two_rate, 1)
    triple_support_win_rate = pct(comp_profile_stats["triple_support"]["wins"], triple_support_count)
    two_two_two_win_rate = pct(comp_profile_stats["two_two_two"]["wins"], two_two_two_count)
    triple_tank_win_rate = pct(comp_profile_stats["triple_tank"]["wins"], triple_tank_count)
    enemy_triple_support_count = enemy_comp_profile_stats["triple_support"]["count"]
    enemy_triple_support_rate = pct(enemy_triple_support_count, total_maps)
    enemy_triple_tank_count = enemy_comp_profile_stats["triple_tank"]["count"]
    enemy_triple_tank_rate = pct(enemy_triple_tank_count, total_maps)
    triple_support_prevalence_diff = round(triple_support_rate - enemy_triple_support_rate, 1)
    comp_winrate_difference = (
        round(triple_support_win_rate - two_two_two_win_rate, 1)
        if triple_support_count and two_two_two_count
        else None
    )

    comp_archetype_labels = {
        "triple_support": "Triple Support",
        "two_two_two": "2-2-2",
        "other": "Other / Flex",
    }
    comp_archetype_rows = []
    for profile_key in ("triple_support", "two_two_two", "other"):
        main_count = comp_profile_stats[profile_key]["count"]
        enemy_count = enemy_comp_profile_stats[profile_key]["count"]
        main_rate = pct(main_count, total_maps)
        enemy_rate = pct(enemy_count, total_maps)
        rate_diff = round(main_rate - enemy_rate, 1)
        comp_archetype_rows.append(
            {
                "profile_key": profile_key,
                "label": comp_archetype_labels.get(profile_key, profile_key.replace("_", " ").title()),
                "main_count": main_count,
                "main_rate": main_rate,
                "main_win_rate": pct(comp_profile_stats[profile_key]["wins"], main_count),
                "enemy_count": enemy_count,
                "enemy_rate": enemy_rate,
                "enemy_win_rate": pct(enemy_comp_profile_stats[profile_key]["wins"], enemy_count),
                "rate_diff": rate_diff,
            }
        )

    return {
        "summary": {
            "total_maps": total_maps,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "overall_win_rate": overall_win_rate,
            "unique_bans": len(ban_rows),
            "unique_enemy_bans": len(enemy_ban_rows),
            "unique_protects": len(protect_rows),
            "unique_heroes": len(hero_rows),
            "unique_maps": len(map_rows),
        },
        "ban_protect_summary": {
            "correlation": ban_protect_correlation,
            "top_hero": top_ban_protect["hero"] if top_ban_protect else "-",
            "top_presence": top_ban_protect["draft_presence"] if top_ban_protect else 0,
        },
        "comp_difference": {
            "triple_support_count": triple_support_count,
            "triple_support_rate": triple_support_rate,
            "triple_support_win_rate": triple_support_win_rate,
            "enemy_triple_support_count": enemy_triple_support_count,
            "enemy_triple_support_rate": enemy_triple_support_rate,
            "triple_support_prevalence_diff": triple_support_prevalence_diff,
            "triple_tank_count": triple_tank_count,
            "triple_tank_rate": triple_tank_rate,
            "triple_tank_win_rate": triple_tank_win_rate,
            "enemy_triple_tank_count": enemy_triple_tank_count,
            "enemy_triple_tank_rate": enemy_triple_tank_rate,
            "two_two_two_count": two_two_two_count,
            "two_two_two_rate": two_two_two_rate,
            "two_two_two_win_rate": two_two_two_win_rate,
            "difference_rate": comp_difference_rate,
            "winrate_difference": comp_winrate_difference,
        },
        "ban_rows": ban_rows[:12],
        "enemy_ban_rows": enemy_ban_rows[:12],
        "ban_position_rows": ban_position_rows,
        "enemy_ban_position_rows": enemy_ban_position_rows,
        "ban_phase_variation": {
            "main": main_ban_variation,
            "enemy": enemy_ban_variation,
        },
        "mirror_rates": mirror_rates,
        "comp_archetype_rows": comp_archetype_rows,
        "ban_diff_rows": ban_diff_rows[:12],
        "ban_next_rows": ban_next_rows[:12],
        "ban_to_protect_rows": ban_to_protect_rows[:12],
        "draft_route_rows": draft_route_rows[:16],
        "second_order_ban_rows": second_order_ban_rows[:12],
        "protect1_influence_rows": protect1_influence_rows[:12],
        "most_likely_ban_route_rows": most_likely_ban_route_rows[:16],
        "lead_to_ban_rows": lead_to_ban_rows[:12],
        "lead_to_protect_rows": lead_to_protect_rows[:12],
        "ban_protect_rows": ban_protect_rows[:12],
        "hero_open_rows": hero_open_rows[:16],
        "protect_rows": protect_rows[:12],
        "hero_rows": hero_rows[:12],
        "map_rows": map_rows[:12],
        "map_draft_rows": map_draft_rows[:12],
    }


