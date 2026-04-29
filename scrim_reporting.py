import csv
import io
import zipfile
from collections import defaultdict


def build_pivot_wr(team_scrims: list[dict], *, attack_defense_maps: set[str] | tuple[str, ...] | list[str]) -> dict:
    """Track hero-switch (pivot) win rates on winning attack rounds for Escort/Hybrid maps.

    A pivot is detected when a player:
      1. Played hero X on attack and the attack was LOST.
      2. In their next recorded attack appearance, played a DIFFERENT hero Y.

    Returns per-player and per-hero-pair pivot stats.
    """
    player_atk_history: dict[str, list[tuple[str, bool]]] = defaultdict(list)

    for scrim in team_scrims:
        for map_entry in scrim.get("maps", []):
            map_name = (map_entry.get("map_name") or "").strip()
            if map_name not in attack_defense_maps:
                continue

            our_atk_raw = map_entry.get("our_attack_score", "")
            enemy_atk_raw = map_entry.get("enemy_attack_score", "")
            if our_atk_raw in ("", None) or enemy_atk_raw in ("", None):
                continue
            try:
                our_atk = int(our_atk_raw)
                enemy_atk = int(enemy_atk_raw)
            except (ValueError, TypeError):
                continue

            atk_won = our_atk > enemy_atk
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in ("team1", "team2"):
                our_team_slot = "team1"

            for section in map_entry.get("comp", []):
                if (section.get("side") or "").strip() != "Attack":
                    continue
                for slot in section.get(our_team_slot, []):
                    hero = (slot.get("hero") or "").strip()
                    player = (slot.get("player") or "").strip()
                    if not hero or not player:
                        continue
                    player_atk_history[player].append((hero, atk_won))

    per_player: dict[str, dict] = {}
    pair_stats: dict[tuple[str, str], dict] = defaultdict(lambda: {"attempts": 0, "wins": 0, "players": set()})

    for player, history in player_atk_history.items():
        p_attempts = 0
        p_wins = 0
        for index in range(1, len(history)):
            prev_hero, prev_won = history[index - 1]
            curr_hero, curr_won = history[index]
            if not prev_won and curr_hero != prev_hero:
                p_attempts += 1
                if curr_won:
                    p_wins += 1
                pair_stats[(prev_hero, curr_hero)]["attempts"] += 1
                if curr_won:
                    pair_stats[(prev_hero, curr_hero)]["wins"] += 1
                pair_stats[(prev_hero, curr_hero)]["players"].add(player)
        if p_attempts:
            per_player[player] = {
                "player": player,
                "pivot_attempts": p_attempts,
                "pivot_wins": p_wins,
                "pivot_wr": round(p_wins / p_attempts * 100, 1),
            }

    per_player_rows = sorted(per_player.values(), key=lambda row: -row["pivot_attempts"])
    per_pair_rows = [
        {
            "from_hero": from_hero,
            "to_hero": to_hero,
            "attempts": stats["attempts"],
            "wins": stats["wins"],
            "win_rate": round(stats["wins"] / stats["attempts"] * 100, 1) if stats["attempts"] else 0,
            "players": sorted(stats["players"]),
        }
        for (from_hero, to_hero), stats in pair_stats.items()
    ]
    per_pair_rows.sort(key=lambda row: (-row["attempts"], row["from_hero"].lower()))

    total_attempts = sum(player["pivot_attempts"] for player in per_player.values())
    total_wins = sum(player["pivot_wins"] for player in per_player.values())
    return {
        "total_attempts": total_attempts,
        "total_wins": total_wins,
        "overall_wr": round(total_wins / total_attempts * 100, 1) if total_attempts else 0,
        "per_player": per_player_rows,
        "per_pair": per_pair_rows,
    }


def build_atk_def_wr(team_scrims: list[dict], *, attack_defense_maps: set[str] | tuple[str, ...] | list[str]) -> dict:
    """Compute attack/defense round win-rate stats for Escort and Hybrid maps."""
    rounds = 0
    eligible_maps = 0
    scored_maps = 0
    total_atk_score = 0
    total_def_conceded = 0
    atk_successes = 0
    def_successes = 0
    full_clears = 0
    full_holds = 0

    per_map: dict[str, dict] = defaultdict(lambda: {
        "rounds": 0, "total_atk": 0, "total_def": 0,
        "atk_successes": 0, "def_successes": 0, "full_clears": 0, "full_holds": 0,
    })
    per_hero: dict[str, dict] = defaultdict(lambda: {
        "atk_apps": 0, "atk_wins": 0, "def_apps": 0, "def_wins": 0,
    })

    for scrim in team_scrims:
        for map_entry in scrim.get("maps", []):
            map_name = (map_entry.get("map_name") or "").strip()
            if map_name not in attack_defense_maps:
                continue
            eligible_maps += 1

            our_atk_raw = map_entry.get("our_attack_score", "")
            enemy_atk_raw = map_entry.get("enemy_attack_score", "")
            if our_atk_raw == "" or our_atk_raw is None or enemy_atk_raw == "" or enemy_atk_raw is None:
                continue
            try:
                our_atk = int(our_atk_raw)
                enemy_atk = int(enemy_atk_raw)
            except (ValueError, TypeError):
                continue

            scored_maps += 1
            rounds += 1
            total_atk_score += our_atk
            total_def_conceded += enemy_atk
            map_stats = per_map[map_name]
            map_stats["rounds"] += 1
            map_stats["total_atk"] += our_atk
            map_stats["total_def"] += enemy_atk

            round_atk_won = our_atk > enemy_atk
            round_def_won = enemy_atk < our_atk

            if round_atk_won:
                atk_successes += 1
                map_stats["atk_successes"] += 1
            if round_def_won:
                def_successes += 1
                map_stats["def_successes"] += 1
            if our_atk >= 3:
                full_clears += 1
                map_stats["full_clears"] += 1
            if enemy_atk == 0:
                full_holds += 1
                map_stats["full_holds"] += 1

            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in ("team1", "team2"):
                our_team_slot = "team1"

            for section in map_entry.get("comp", []):
                section_side = (section.get("side") or "").strip()
                heroes_in_section = [
                    (slot.get("hero") or "").strip()
                    for slot in section.get(our_team_slot, [])
                    if (slot.get("hero") or "").strip()
                ]
                for hero in heroes_in_section:
                    if section_side == "Attack":
                        per_hero[hero]["atk_apps"] += 1
                        if round_atk_won:
                            per_hero[hero]["atk_wins"] += 1
                    elif section_side == "Defense":
                        per_hero[hero]["def_apps"] += 1
                        if round_def_won:
                            per_hero[hero]["def_wins"] += 1

    per_map_rows = []
    for map_name, stats in per_map.items():
        rounds_played = stats["rounds"]
        per_map_rows.append({
            "map_name": map_name,
            "rounds": rounds_played,
            "atk_avg": round(stats["total_atk"] / rounds_played, 2) if rounds_played else 0,
            "def_avg": round(stats["total_def"] / rounds_played, 2) if rounds_played else 0,
            "atk_success_rate": round(stats["atk_successes"] / rounds_played * 100, 1) if rounds_played else 0,
            "def_success_rate": round(stats["def_successes"] / rounds_played * 100, 1) if rounds_played else 0,
            "full_clear_rate": round(stats["full_clears"] / rounds_played * 100, 1) if rounds_played else 0,
            "full_hold_rate": round(stats["full_holds"] / rounds_played * 100, 1) if rounds_played else 0,
        })
    per_map_rows.sort(key=lambda row: (-row["rounds"], row["map_name"].lower()))

    per_hero_rows = []
    for hero, stats in per_hero.items():
        total_apps = stats["atk_apps"] + stats["def_apps"]
        if not total_apps:
            continue
        per_hero_rows.append({
            "hero": hero,
            "atk_apps": stats["atk_apps"],
            "atk_win_rate": round(stats["atk_wins"] / stats["atk_apps"] * 100, 1) if stats["atk_apps"] else None,
            "def_apps": stats["def_apps"],
            "def_win_rate": round(stats["def_wins"] / stats["def_apps"] * 100, 1) if stats["def_apps"] else None,
        })
    per_hero_rows.sort(key=lambda row: -(row["atk_apps"] + row["def_apps"]))

    return {
        "rounds": rounds,
        "eligible_maps": eligible_maps,
        "scored_maps": scored_maps,
        "missing_score_maps": max(eligible_maps - scored_maps, 0),
        "atk_avg": round(total_atk_score / rounds, 2) if rounds else 0,
        "def_avg": round(total_def_conceded / rounds, 2) if rounds else 0,
        "atk_success_rate": round(atk_successes / rounds * 100, 1) if rounds else 0,
        "def_success_rate": round(def_successes / rounds * 100, 1) if rounds else 0,
        "full_clear_rate": round(full_clears / rounds * 100, 1) if rounds else 0,
        "full_hold_rate": round(full_holds / rounds * 100, 1) if rounds else 0,
        "per_map": per_map_rows,
        "per_hero": per_hero_rows,
    }


def build_scrim_log_rows(
    team_scrims: list,
    *,
    team_slots: list[str] | tuple[str, ...],
    canonical_draft_hero,
    hero_match_key,
    hero_role,
    opposite_team_slot,
    get_map_outcome_for_slot,
    infer_result_from_score_text,
    split_score_pair,
) -> dict:
    """Build flat per-map rows for the Scrims tab quick-scan view."""
    role_order = {"Vanguard": 0, "Duelist": 1, "Strategist": 2}

    def sort_heroes(raw_heroes: list[str]) -> list[str]:
        unique_by_key: dict[str, str] = {}
        for raw_hero in raw_heroes:
            canonical = canonical_draft_hero(raw_hero)
            key = hero_match_key(canonical)
            if not key:
                continue
            unique_by_key[key] = canonical
        return sorted(unique_by_key.values(), key=lambda hero: (role_order.get(hero_role(hero), 99), hero.lower()))

    rows: list[dict] = []
    opponents: set[str] = set()
    all_maps: set[str] = set()
    all_bans: set[str] = set()
    all_duelists: set[str] = set()
    all_seasons: set[str] = set()

    for scrim in team_scrims:
        scrim_id = scrim.get("id")
        scrim_date = (scrim.get("scrim_date", "") or "").strip()
        opponent_name = ((scrim.get("enemy_team", "") or "").strip() or (scrim.get("opponent", "") or "").strip() or "Opponent")
        season = (scrim.get("season", "") or "").strip()
        opponents.add(opponent_name)
        if season:
            all_seasons.add(season)

        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in team_slots:
                our_team_slot = "team1"
            enemy_team_slot = opposite_team_slot(our_team_slot)

            map_name = (map_entry.get("map_name", "") or "").strip() or "Unknown Map"
            map_type = (map_entry.get("map_type", "Standard") or "Standard").strip()
            result = get_map_outcome_for_slot(map_entry, our_team_slot)
            score = (map_entry.get("score", "") or "").strip()

            draft = map_entry.get("draft", {})
            our_draft = draft.get(our_team_slot, {}) if isinstance(draft, dict) else {}
            enemy_draft = draft.get(enemy_team_slot, {}) if isinstance(draft, dict) else {}

            our_bans = [hero for hero in [(our_draft.get("ban1", "") or "").strip(), (our_draft.get("ban2", "") or "").strip(), (our_draft.get("ban3", "") or "").strip(), (our_draft.get("ban4", "") or "").strip()] if hero]
            enemy_bans = [hero for hero in [(enemy_draft.get("ban1", "") or "").strip(), (enemy_draft.get("ban2", "") or "").strip(), (enemy_draft.get("ban3", "") or "").strip(), (enemy_draft.get("ban4", "") or "").strip()] if hero]
            our_protects = [hero for hero in [(our_draft.get("protect1", "") or "").strip(), (our_draft.get("protect2", "") or "").strip()] if hero]
            enemy_protects = [hero for hero in [(enemy_draft.get("protect1", "") or "").strip(), (enemy_draft.get("protect2", "") or "").strip()] if hero]
            all_bans.update(our_bans)
            all_bans.update(enemy_bans)

            our_raw: list[str] = []
            enemy_raw: list[str] = []
            for section in map_entry.get("comp", []):
                our_raw.extend((slot.get("hero", "") or "").strip() for slot in section.get(our_team_slot, []) if (slot.get("hero", "") or "").strip())
                enemy_raw.extend((slot.get("hero", "") or "").strip() for slot in section.get(enemy_team_slot, []) if (slot.get("hero", "") or "").strip())

            our_heroes = sort_heroes(our_raw)
            enemy_heroes = sort_heroes(enemy_raw)
            our_duelists = [hero for hero in our_heroes if hero_role(hero) == "Duelist"]
            all_duelists.update(our_duelists)
            all_maps.add(map_name)

            sections_data = []
            for section_index, section in enumerate(map_entry.get("comp", []), start=1):
                sec_label = (section.get("submap", "") or "").strip() or f"Round {section_index}"
                sec_score = (section.get("score", "") or "").strip()
                sec_result_raw = (section.get("result", "") or "").strip()
                sec_side = (section.get("side", "") or "").strip()
                sec_result = infer_result_from_score_text(sec_score, slot=our_team_slot)
                if not sec_result and sec_result_raw in ("Win", "Loss"):
                    sec_result = "Loss" if our_team_slot == "team2" and sec_result_raw == "Win" else "Win" if our_team_slot == "team2" and sec_result_raw == "Loss" else sec_result_raw
                elif not sec_result:
                    sec_result = sec_result_raw

                our_slots = [{"hero": (slot.get("hero", "") or "").strip(), "player": (slot.get("player", "") or "").strip()} for slot in section.get(our_team_slot, [])]
                enemy_slots = [{"hero": (slot.get("hero", "") or "").strip(), "player": (slot.get("player", "") or "").strip()} for slot in section.get(enemy_team_slot, [])]
                display_score = sec_score
                if our_team_slot == "team2" and sec_score:
                    left, right = split_score_pair(sec_score)
                    if left and right:
                        display_score = f"{right}-{left}"
                sections_data.append({
                    "label": sec_label,
                    "score": display_score,
                    "result": sec_result,
                    "side": sec_side,
                    "our_slots": our_slots,
                    "enemy_slots": enemy_slots,
                })

            rows.append({
                "scrim_id": scrim_id,
                "scrim_date": scrim_date,
                "opponent_name": opponent_name,
                "season": season,
                "patch": season,
                "map_name": map_name,
                "map_type": map_type,
                "result": result,
                "score": score,
                "our_team_slot": our_team_slot,
                "our_bans": our_bans,
                "our_protects": our_protects,
                "enemy_bans": enemy_bans,
                "enemy_protects": enemy_protects,
                "our_heroes": our_heroes,
                "enemy_heroes": enemy_heroes,
                "our_duelists": our_duelists,
                "sections": sections_data,
            })

    rows.sort(key=lambda row: (row.get("scrim_date", ""), row.get("opponent_name", "").lower()), reverse=True)
    return {
        "rows": rows,
        "filter_options": {
            "opponents": sorted(opponents),
            "maps": sorted(all_maps),
            "bans": sorted(all_bans),
            "duelists": sorted(all_duelists),
            "seasons": sorted(all_seasons),
        },
    }


def filter_scrim_log_rows(rows: list[dict], *, opponent: str = "", map_name: str = "", ban: str = "", duelist: str = "") -> list[dict]:
    selected_opponent = (opponent or "").strip()
    selected_map = (map_name or "").strip()
    selected_ban = (ban or "").strip()
    selected_duelist = (duelist or "").strip()

    filtered_rows: list[dict] = []
    for row in rows:
        if selected_opponent and row.get("opponent_name", "") != selected_opponent:
            continue
        if selected_map and row.get("map_name", "") != selected_map:
            continue
        if selected_ban and selected_ban not in row.get("our_bans", []) + row.get("enemy_bans", []):
            continue
        if selected_duelist and selected_duelist not in row.get("our_duelists", []):
            continue
        filtered_rows.append(row)
    return filtered_rows


def build_scrim_log_export_archive(
    team_name: str,
    rows: list[dict],
    *,
    normalize_match_team_slot,
    opposite_team_slot,
    simulator_slot_order: tuple[str, ...] | list[str],
) -> bytes:
    def winner_label(result: str, our_label: str, their_label: str) -> str:
        if result == "Win":
            return our_label
        if result == "Loss":
            return their_label
        return ""

    def padded_values(values: list[str], target_size: int) -> list[str]:
        cleaned = [(value or "").strip() for value in values if (value or "").strip()]
        return cleaned[:target_size] + [""] * max(0, target_size - len(cleaned))

    def draft_action_rows(match_id: str, row: dict) -> list[list[str]]:
        our_team_slot = normalize_match_team_slot(row.get("our_team_slot", "team1"))
        their_team_slot = opposite_team_slot(our_team_slot)
        our_bans = padded_values(row.get("our_bans", []), 4)
        their_bans = padded_values(row.get("enemy_bans", []), 4)
        our_protects = padded_values(row.get("our_protects", []), 2)
        their_protects = padded_values(row.get("enemy_protects", []), 2)
        slot_sources = {
            f"{our_team_slot}_ban1": our_bans[0],
            f"{our_team_slot}_ban2": our_bans[1],
            f"{our_team_slot}_ban3": our_bans[2],
            f"{our_team_slot}_ban4": our_bans[3],
            f"{our_team_slot}_protect1": our_protects[0],
            f"{our_team_slot}_protect2": our_protects[1],
            f"{their_team_slot}_ban1": their_bans[0],
            f"{their_team_slot}_ban2": their_bans[1],
            f"{their_team_slot}_ban3": their_bans[2],
            f"{their_team_slot}_ban4": their_bans[3],
            f"{their_team_slot}_protect1": their_protects[0],
            f"{their_team_slot}_protect2": their_protects[1],
        }
        team_labels = {our_team_slot: "Our", their_team_slot: "Their"}
        output_rows: list[list[str]] = []
        for order_index, slot_name in enumerate(simulator_slot_order, start=1):
            side_name, action_name = slot_name.split("_", 1)
            hero_name = (slot_sources.get(slot_name, "") or "").strip()
            if not hero_name:
                continue
            action_type = "Protect" if action_name.startswith("protect") else "Ban"
            output_rows.append([match_id, str(order_index), team_labels.get(side_name, side_name.title()), action_type, hero_name])
        return output_rows

    def player_hero_rows(match_id: str, team_side: str, slots: list[dict]) -> list[list[str]]:
        output_rows: list[list[str]] = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            player_name = (slot.get("player", "") or "").strip()
            hero_name = (slot.get("hero", "") or "").strip()
            if not player_name and not hero_name:
                continue
            output_rows.append([match_id, team_side, player_name, hero_name])
        return output_rows

    maps_buffer = io.StringIO(newline="")
    maps_writer = csv.writer(maps_buffer)
    maps_writer.writerow(["match_id", "date", "our_team", "their_team", "patch", "map", "map_type", "round", "map_winner", "map_result", "map_score", "round_winner", "round_result", "round_score", "round_side"])

    draft_buffer = io.StringIO(newline="")
    draft_writer = csv.writer(draft_buffer)
    draft_writer.writerow(["match_id", "action_order", "acting_team", "action_type", "hero"])

    player_buffer = io.StringIO(newline="")
    player_writer = csv.writer(player_buffer)
    player_writer.writerow(["match_id", "team_side", "player", "hero"])

    for row_index, row in enumerate(rows, start=1):
        our_team_name = (team_name or "").strip() or "Our Team"
        their_team_name = (row.get("opponent_name", "") or "").strip() or "Their Team"
        map_result = (row.get("result", "") or "").strip()
        map_score = (row.get("score", "") or "").strip()
        sections = row.get("sections", [])
        if sections:
            for section_index, section in enumerate(sections, start=1):
                match_id = f"S{row.get('scrim_id') or 'x'}-M{row_index}-R{section_index}"
                round_result = (section.get("result", "") or "").strip()
                maps_writer.writerow([
                    match_id,
                    (row.get("scrim_date", "") or "").strip(),
                    our_team_name,
                    their_team_name,
                    (row.get("patch", row.get("season", "")) or "").strip(),
                    (row.get("map_name", "") or "").strip(),
                    (row.get("map_type", "") or "").strip(),
                    (section.get("label", "") or "").strip(),
                    winner_label(map_result, our_team_name, their_team_name),
                    map_result,
                    map_score,
                    winner_label(round_result, our_team_name, their_team_name),
                    round_result,
                    (section.get("score", "") or "").strip(),
                    (section.get("side", "") or "").strip(),
                ])
                for draft_row in draft_action_rows(match_id, row):
                    draft_writer.writerow(draft_row)
                for assignment_row in player_hero_rows(match_id, "Our", section.get("our_slots", [])):
                    player_writer.writerow(assignment_row)
                for assignment_row in player_hero_rows(match_id, "Their", section.get("enemy_slots", [])):
                    player_writer.writerow(assignment_row)
        else:
            match_id = f"S{row.get('scrim_id') or 'x'}-M{row_index}-R0"
            maps_writer.writerow([
                match_id,
                (row.get("scrim_date", "") or "").strip(),
                our_team_name,
                their_team_name,
                (row.get("patch", row.get("season", "")) or "").strip(),
                (row.get("map_name", "") or "").strip(),
                (row.get("map_type", "") or "").strip(),
                "",
                winner_label(map_result, our_team_name, their_team_name),
                map_result,
                map_score,
                "",
                "",
                "",
                "",
            ])
            for draft_row in draft_action_rows(match_id, row):
                draft_writer.writerow(draft_row)
            for hero_name in row.get("our_heroes", []):
                player_writer.writerow([match_id, "Our", "", (hero_name or "").strip()])
            for hero_name in row.get("enemy_heroes", []):
                player_writer.writerow([match_id, "Their", "", (hero_name or "").strip()])

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("maps.csv", "\ufeff" + maps_buffer.getvalue())
        archive.writestr("draft_actions.csv", "\ufeff" + draft_buffer.getvalue())
        archive.writestr("player_heroes.csv", "\ufeff" + player_buffer.getvalue())
    return archive_buffer.getvalue()