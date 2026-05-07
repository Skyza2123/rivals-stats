from collections import defaultdict


def build_team_detail_matchup_context(
    team_scrims: list[dict],
    players: list[dict],
    *,
    target_team_id: int | None = None,
    target_team_name: str = "",
    team_slots: list[str] | tuple[str, ...],
    canonical_draft_hero,
    hero_match_key,
    hero_role,
    opposite_team_slot,
    get_map_outcome_for_slot,
) -> dict:
    role_order = {"Vanguard": 0, "Duelist": 1, "Strategist": 2}
    target_team_name_normalized = (target_team_name or "").strip().lower()

    def normalize_slot(raw_slot: str | None) -> str:
        raw_slot = (raw_slot or "").strip()
        return raw_slot if raw_slot in team_slots else "team1"

    def same_team_id(raw_value) -> bool:
        if target_team_id is None or raw_value in (None, ""):
            return False
        try:
            return int(raw_value) == int(target_team_id)
        except (TypeError, ValueError):
            return False

    def side_label(scrim: dict, map_entry: dict, slot: str) -> str:
        slot = normalize_slot(slot)
        label = (
            (map_entry.get(f"{slot}_name", "") or "").strip()
            or (scrim.get(f"{slot}_name", "") or "").strip()
        )
        if label:
            return label

        scrim_team_slot = normalize_slot(scrim.get("team_slot", "team1"))
        if slot == scrim_team_slot:
            return (scrim.get("team_name", "") or "").strip() or "Our Team"
        return (
            (scrim.get("enemy_team", "") or "").strip()
            or (scrim.get("opponent", "") or "").strip()
            or "Opponent"
        )

    def resolve_target_slot(scrim: dict, map_entry: dict) -> str:
        if target_team_id is not None:
            if same_team_id(map_entry.get("team1_id")) or same_team_id(scrim.get("team1_id")):
                return "team1"
            if same_team_id(map_entry.get("team2_id")) or same_team_id(scrim.get("team2_id")):
                return "team2"
            if same_team_id(scrim.get("team_id")):
                return normalize_slot(map_entry.get("our_team_slot") or scrim.get("team_slot", "team1"))
            if same_team_id(scrim.get("enemy_team_id")):
                return opposite_team_slot(normalize_slot(map_entry.get("our_team_slot") or scrim.get("team_slot", "team1")))

        if target_team_name_normalized:
            for slot in team_slots:
                if side_label(scrim, map_entry, slot).strip().lower() == target_team_name_normalized:
                    return slot
            if (scrim.get("team_name", "") or "").strip().lower() == target_team_name_normalized:
                return normalize_slot(map_entry.get("our_team_slot") or scrim.get("team_slot", "team1"))
            if (
                (scrim.get("enemy_team", "") or "").strip().lower() == target_team_name_normalized
                or (scrim.get("opponent", "") or "").strip().lower() == target_team_name_normalized
            ):
                return opposite_team_slot(normalize_slot(map_entry.get("our_team_slot") or scrim.get("team_slot", "team1")))

        return normalize_slot(map_entry.get("our_team_slot") or scrim.get("team_slot", "team1"))

    def sorted_heroes_for_matchup(raw_heroes: list[str]) -> list[str]:
        unique_by_key: dict[str, str] = {}
        for raw_hero in raw_heroes:
            canonical = canonical_draft_hero(raw_hero)
            key = hero_match_key(canonical)
            if not key:
                continue
            unique_by_key[key] = canonical

        return sorted(
            unique_by_key.values(),
            key=lambda hero_name: (
                role_order.get(hero_role(hero_name), 99),
                hero_name.lower(),
            ),
        )

    matchup_rows = []
    matchup_opponents = set()
    matchup_maps = set()
    matchup_map_totals = defaultdict(int)
    matchup_wins = 0
    matchup_losses = 0
    matchup_other_results = 0

    for scrim in team_scrims:
        scrim_date = (scrim.get("scrim_date", "") or "").strip()

        for map_entry in scrim.get("maps", []):
            our_team_slot = resolve_target_slot(scrim, map_entry)
            enemy_team_slot = opposite_team_slot(our_team_slot)
            opponent_name = side_label(scrim, map_entry, enemy_team_slot)
            matchup_opponents.add(opponent_name)

            map_name = (map_entry.get("map_name", "") or "").strip() or "Unknown Map"
            result = get_map_outcome_for_slot(map_entry, our_team_slot)

            if result == "Win":
                matchup_wins += 1
            elif result == "Loss":
                matchup_losses += 1
            else:
                matchup_other_results += 1

            matchup_maps.add(map_name)
            matchup_map_totals[map_name] += 1

            our_raw_heroes = []
            enemy_raw_heroes = []
            for section in map_entry.get("comp", []):
                our_raw_heroes.extend(
                    [
                        (slot.get("hero", "") or "").strip()
                        for slot in section.get(our_team_slot, [])
                        if (slot.get("hero", "") or "").strip()
                    ]
                )
                enemy_raw_heroes.extend(
                    [
                        (slot.get("hero", "") or "").strip()
                        for slot in section.get(enemy_team_slot, [])
                        if (slot.get("hero", "") or "").strip()
                    ]
                )

            matchup_rows.append(
                {
                    "scrim_date": scrim_date,
                    "opponent_name": opponent_name,
                    "our_team_name": side_label(scrim, map_entry, our_team_slot),
                    "map_name": map_name,
                    "result": result,
                    "our_heroes": sorted_heroes_for_matchup(our_raw_heroes),
                    "enemy_heroes": sorted_heroes_for_matchup(enemy_raw_heroes),
                }
            )

    matchup_rows.sort(
        key=lambda row: (
            row.get("scrim_date", ""),
            row.get("opponent_name", "").lower(),
            row.get("map_name", "").lower(),
        ),
        reverse=True,
    )

    matchup_summary = {
        "total_maps": len(matchup_rows),
        "wins": matchup_wins,
        "losses": matchup_losses,
        "other_results": matchup_other_results,
        "decided_maps": matchup_wins + matchup_losses,
        "win_rate": round((matchup_wins / (matchup_wins + matchup_losses)) * 100, 1) if (matchup_wins + matchup_losses) else 0,
        "unique_opponents": len(matchup_opponents),
        "unique_maps": len(matchup_maps),
    }

    matrix_map_columns = [
        map_name
        for map_name, _count in sorted(
            matchup_map_totals.items(),
            key=lambda item: (-item[1], item[0].lower()),
        )
    ]
    matrix_rows = []
    for player in players:
        per_map = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
        for scrim in team_scrims:
            for map_entry in scrim.get("maps", []):
                our_team_slot = resolve_target_slot(scrim, map_entry)

                player_found = False
                for section in map_entry.get("comp", []):
                    for slot in section.get(our_team_slot, []):
                        if (slot.get("player", "") or "").strip().lower() == player["name"].strip().lower():
                            player_found = True
                            break
                    if player_found:
                        break

                if not player_found:
                    continue

                map_name = (map_entry.get("map_name", "") or "").strip() or "Unknown Map"
                result = get_map_outcome_for_slot(map_entry, our_team_slot)
                per_map[map_name]["maps"] += 1
                if result == "Win":
                    per_map[map_name]["wins"] += 1
                    per_map[map_name]["decided"] += 1
                elif result == "Loss":
                    per_map[map_name]["losses"] += 1
                    per_map[map_name]["decided"] += 1
                else:
                    per_map[map_name]["unresolved"] += 1

        cells = []
        total_maps = 0
        total_wins = 0
        total_losses = 0
        total_decided = 0
        total_unresolved = 0
        for map_name in matrix_map_columns:
            stats = per_map.get(map_name)
            if not stats or not stats["maps"]:
                cells.append(None)
                continue

            total_maps += stats["maps"]
            total_wins += stats["wins"]
            total_losses += stats["losses"]
            total_decided += stats["decided"]
            total_unresolved += stats["unresolved"]
            cells.append(
                {
                    "maps": stats["maps"],
                    "decided_maps": stats["decided"],
                    "unresolved_maps": stats["unresolved"],
                    "wins": stats["wins"],
                    "losses": stats["losses"],
                    "win_rate": round((stats["wins"] / stats["decided"]) * 100, 1) if stats["decided"] else 0,
                }
            )

        matrix_rows.append(
            {
                "player_id": player["id"],
                "player_name": player["name"],
                "role": player.get("role", ""),
                "cells": cells,
                "summary": {
                    "maps": total_maps,
                    "decided_maps": total_decided,
                    "unresolved_maps": total_unresolved,
                    "wins": total_wins,
                    "losses": total_losses,
                    "win_rate": round((total_wins / total_decided) * 100, 1) if total_decided else 0,
                },
            }
        )

    matrix_rows.sort(
        key=lambda row: (
            role_order.get((row.get("role", "") or "").strip(), 99),
            -row["summary"]["maps"],
            row["player_name"].lower(),
        )
    )

    return {
        "matchup_summary": matchup_summary,
        "matchup_rows": matchup_rows,
        "matrix_map_columns": matrix_map_columns,
        "matrix_rows": matrix_rows,
    }
