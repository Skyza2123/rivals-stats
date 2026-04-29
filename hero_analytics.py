import math
from collections import defaultdict


def _normalize_team_slot(team_slot: str, team_slots: list[str] | tuple[str, ...]) -> str:
    return team_slot if team_slot in team_slots else "team1"


def build_player_hero_map_breakdown(
    player_name: str,
    scrims: list[dict],
    *,
    team_slots: list[str] | tuple[str, ...],
    canonical_draft_hero,
    get_map_outcome_for_slot,
    map_modes: dict[str, str],
    get_map_image_url,
) -> dict:
    target_name = (player_name or "").strip().lower()
    if not target_name:
        return {
            "hero_rows": [],
            "map_rows": [],
        }

    hero_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    map_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = _normalize_team_slot(map_entry.get("our_team_slot", "team1"), team_slots)
            sections = map_entry.get("comp", [])

            played_submaps = sum(
                1 for section in sections
                if section.get("submap") and any(
                    (slot.get("hero") or "").strip()
                    for slot in section.get("team1", []) + section.get("team2", [])
                )
            )

            player_found = False
            hero_weights: dict[str, float] = {}
            for section in sections:
                weight = 1.0 / played_submaps if section.get("submap") and played_submaps > 0 else 0.5
                for slot in section.get(our_team_slot, []):
                    if (slot.get("player", "") or "").strip().lower() != target_name:
                        continue
                    player_found = True
                    hero_name = canonical_draft_hero(slot.get("hero", ""))
                    if hero_name:
                        hero_weights[hero_name] = hero_weights.get(hero_name, 0.0) + weight

            if not player_found:
                continue

            map_name = (map_entry.get("map_name", "") or "").strip()
            result = get_map_outcome_for_slot(map_entry, our_team_slot)
            if map_name:
                map_stats[map_name]["maps"] += 1
                if result == "Win":
                    map_stats[map_name]["wins"] += 1
                    map_stats[map_name]["decided"] += 1
                elif result == "Loss":
                    map_stats[map_name]["losses"] += 1
                    map_stats[map_name]["decided"] += 1
                else:
                    map_stats[map_name]["unresolved"] += 1

            for hero_name, weight in hero_weights.items():
                hero_stats[hero_name]["maps"] += weight
                if result == "Win":
                    hero_stats[hero_name]["wins"] += weight
                    hero_stats[hero_name]["decided"] += weight
                elif result == "Loss":
                    hero_stats[hero_name]["losses"] += weight
                    hero_stats[hero_name]["decided"] += weight
                else:
                    hero_stats[hero_name]["unresolved"] += weight

    hero_rows = []
    for hero_name, stats in hero_stats.items():
        raw_decided_maps = stats["decided"]
        hero_rows.append(
            {
                "hero": hero_name,
                "maps": round(stats["maps"], 2),
                "decided_maps": round(raw_decided_maps, 2),
                "unresolved_maps": stats["unresolved"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round((stats["wins"] / raw_decided_maps) * 100, 1) if raw_decided_maps else 0,
            }
        )
    hero_rows.sort(key=lambda row: (row["maps"], row["win_rate"]), reverse=True)

    map_rows = []
    for map_name, stats in map_stats.items():
        decided_maps = stats["decided"]
        map_rows.append(
            {
                "map_name": map_name,
                "mode": map_modes.get(map_name, "Other"),
                "maps": stats["maps"],
                "decided_maps": decided_maps,
                "unresolved_maps": stats["unresolved"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round((stats["wins"] / decided_maps) * 100, 1) if decided_maps else 0,
                "image": get_map_image_url(map_name),
            }
        )
    map_rows.sort(key=lambda row: (row["maps"], row["win_rate"]), reverse=True)

    return {
        "hero_rows": hero_rows,
        "map_rows": map_rows,
    }


def build_hero_usage_timeline(
    team_scrims: list[dict],
    top_heroes: list[str],
    *,
    team_slots: list[str] | tuple[str, ...],
    canonical_map_hero_instances,
) -> dict:
    labels = []
    series_map = {hero: [] for hero in top_heroes}
    sorted_scrims = sorted(team_scrims, key=lambda scrim: (scrim.get("scrim_date", ""), scrim.get("id", 0)))

    for scrim in sorted_scrims:
        maps = scrim.get("maps", [])
        if not maps:
            continue

        hero_instance_counts = {hero: 0 for hero in top_heroes}
        total_instances = 0
        for map_entry in maps:
            our_team_slot = _normalize_team_slot(map_entry.get("our_team_slot", "team1"), team_slots)
            map_instances = canonical_map_hero_instances(map_entry, our_team_slot)
            total_instances += len(map_instances)
            for hero_name in map_instances:
                if hero_name in hero_instance_counts:
                    hero_instance_counts[hero_name] += 1

        labels.append(f"{scrim.get('scrim_date', '')} vs {scrim.get('enemy_team') or scrim.get('opponent') or 'Unknown'}")
        for hero in top_heroes:
            series_map[hero].append(round((hero_instance_counts[hero] / total_instances) * 100, 1) if total_instances else 0)

    return {
        "labels": labels,
        "series": [{"hero": hero, "values": series_map[hero]} for hero in top_heroes],
    }


def build_team_hero_profile(
    team_scrims: list[dict],
    players: list[dict],
    *,
    team_slots: list[str] | tuple[str, ...],
    canonical_draft_hero,
    get_map_outcome_for_slot,
) -> dict:
    role_order = {"Vanguard": 0, "Duelist": 1, "Strategist": 2, "Flex": 3}
    hero_map_stats = defaultdict(lambda: {"appearances": 0, "wins": 0, "losses": 0, "players": set()})
    player_instance_totals = defaultdict(int)
    player_hero_counts = defaultdict(lambda: defaultdict(int))
    player_hero_wins = defaultdict(lambda: defaultdict(int))
    tracked_maps = 0

    for scrim in team_scrims:
        for map_entry in scrim.get("maps", []):
            tracked_maps += 1
            our_team_slot = _normalize_team_slot(map_entry.get("our_team_slot", "team1"), team_slots)
            result = get_map_outcome_for_slot(map_entry, our_team_slot)
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                lineup = section.get(our_team_slot, [])
                if not isinstance(lineup, list):
                    continue

                for slot in lineup:
                    if not isinstance(slot, dict):
                        continue
                    hero_name = canonical_draft_hero(slot.get("hero", ""))
                    player_name = (slot.get("player", "") or "").strip()
                    if not hero_name:
                        continue

                    hero_map_stats[hero_name]["appearances"] += 1
                    if result == "Win":
                        hero_map_stats[hero_name]["wins"] += 1
                    elif result == "Loss":
                        hero_map_stats[hero_name]["losses"] += 1

                    if player_name:
                        player_key = player_name.lower()
                        hero_map_stats[hero_name]["players"].add(player_name)
                        player_instance_totals[player_key] += 1
                        player_hero_counts[player_key][hero_name] += 1
                        if result == "Win":
                            player_hero_wins[player_key][hero_name] += 1

    hero_rows = []
    total_hero_instances = 0
    for hero_name, stats in hero_map_stats.items():
        appearances = stats["appearances"]
        total_hero_instances += appearances
        hero_rows.append(
            {
                "hero": hero_name,
                "appearances": appearances,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round((stats["wins"] / appearances) * 100, 1) if appearances else 0,
                "usage_rate": 0,
                "player_count": len([name for name in stats["players"] if name]),
            }
        )
    for row in hero_rows:
        row["usage_rate"] = round((row["appearances"] / total_hero_instances) * 100, 1) if total_hero_instances else 0
    hero_rows.sort(key=lambda row: (row["appearances"], row["win_rate"], row["hero"].lower()), reverse=True)

    effective_pool_threshold = max(2, math.ceil(tracked_maps * 0.1)) if tracked_maps else 0
    effective_pool = sum(1 for row in hero_rows if row["appearances"] >= effective_pool_threshold) if effective_pool_threshold else 0

    if len(hero_rows) > 1 and total_hero_instances:
        entropy = 0.0
        for row in hero_rows:
            share = row["appearances"] / total_hero_instances
            if share > 0:
                entropy -= share * math.log(share)
        diversity_score = round((entropy / math.log(len(hero_rows))) * 100, 1)
    else:
        diversity_score = 0.0

    specialists = []
    top_hero_names = [row["hero"] for row in hero_rows[:15]]
    heatmap_rows = []
    ordered_players = sorted(
        players,
        key=lambda row: (
            role_order.get((row.get("role", "") or "").strip(), 99),
            (row.get("name", "") or "").strip().lower(),
        ),
    )

    for player in ordered_players:
        player_name = (player.get("name", "") or "").strip()
        if not player_name:
            continue

        player_key = player_name.lower()
        total_appearances = player_instance_totals.get(player_key, 0)
        hero_counts = player_hero_counts.get(player_key, {})
        sorted_hero_rows = [
            {
                "hero": hero_name,
                "appearances": count,
                "rate": round((count / total_appearances) * 100, 1) if total_appearances else 0,
                "win_rate": round((player_hero_wins[player_key][hero_name] / count) * 100, 1) if count else 0,
            }
            for hero_name, count in sorted(hero_counts.items(), key=lambda item: (item[1], item[0].lower()), reverse=True)
        ]

        top_row = sorted_hero_rows[0] if sorted_hero_rows else None
        top_two_rate = round((sum(row["appearances"] for row in sorted_hero_rows[:2]) / total_appearances) * 100, 1) if total_appearances else 0
        if top_row and total_appearances >= 3 and (top_row["rate"] >= 45 or top_two_rate >= 70):
            specialists.append(
                {
                    "player_id": player.get("id"),
                    "player_name": player_name,
                    "role": (player.get("role", "") or "").strip(),
                    "appearances": total_appearances,
                    "focus_hero": top_row["hero"],
                    "focus_appearances": top_row["appearances"],
                    "focus_rate": top_row["rate"],
                    "top_two_rate": top_two_rate,
                    "unique_heroes": len(sorted_hero_rows),
                    "hero_rows": sorted_hero_rows[:3],
                }
            )

        heatmap_cells = []
        active_heroes = 0
        for hero_name in top_hero_names:
            count = hero_counts.get(hero_name, 0)
            rate = round((count / total_appearances) * 100, 1) if total_appearances else 0
            win_rate = round((player_hero_wins[player_key][hero_name] / count) * 100, 1) if count else 0
            intensity = 0
            if count and total_appearances:
                intensity = max(16, min(100, int(round(rate))))
                active_heroes += 1
            heatmap_cells.append(
                {
                    "count": count,
                    "rate": rate,
                    "win_rate": win_rate,
                    "intensity": intensity,
                }
            )

        heatmap_rows.append(
            {
                "player_id": player.get("id"),
                "player_name": player_name,
                "role": (player.get("role", "") or "").strip(),
                "appearances": total_appearances,
                "active_heroes": active_heroes,
                "cells": heatmap_cells,
            }
        )

    specialists.sort(
        key=lambda row: (
            row["focus_appearances"],
            row["focus_rate"],
            row["appearances"],
            row["player_name"].lower(),
        ),
        reverse=True,
    )

    return {
        "summary": {
            "tracked_maps": tracked_maps,
            "total_instances": total_hero_instances,
            "total_heroes": len(hero_rows),
            "effective_pool": effective_pool,
            "effective_pool_threshold": effective_pool_threshold,
            "diversity_score": diversity_score,
            "specialist_count": len(specialists),
        },
        "hero_rows": hero_rows,
        "top_heroes": hero_rows[:15],
        "specialists": specialists,
        "heatmap_columns": top_hero_names,
        "heatmap_rows": heatmap_rows,
    }