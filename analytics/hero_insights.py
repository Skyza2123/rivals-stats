# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def flip_result(result: str) -> str:
    if result == "Win":
        return "Loss"
    if result == "Loss":
        return "Win"
    return result


def to_enemy_perspective_scrims(scrims: list[dict]) -> list[dict]:
    transformed_scrims = []
    for scrim in scrims:
        transformed_maps = []
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            enemy_team_slot = opposite_team_slot(our_team_slot)

            transformed_map = dict(map_entry)
            transformed_map["our_team_slot"] = enemy_team_slot
            transformed_map["result"] = flip_result(map_entry.get("result", ""))
            transformed_map["score"] = flip_score_text(map_entry.get("score", ""))
            transformed_sections = []
            for section in map_entry.get("comp", []):
                transformed_section = dict(section)
                transformed_section["score"] = flip_score_text(section.get("score", ""))
                transformed_sections.append(transformed_section)
            transformed_map["comp"] = transformed_sections
            transformed_maps.append(transformed_map)

        transformed_scrim = dict(scrim)
        transformed_scrim["maps"] = transformed_maps
        transformed_scrims.append(transformed_scrim)

    return transformed_scrims


def build_map_mode_breakdown(scrims: list[dict]) -> tuple[list[dict], list[dict], dict | None, dict | None]:
    map_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    mode_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    map_type_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    opponent_records = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0, "decided": 0, "unresolved": 0})
    recent_map_visual_rows: list[dict] = []
    map_timeline_targets: dict[str, int] = {}
    side_score_records = defaultdict(
        lambda: {
            "Attack": {"sum": 0.0, "count": 0},
            "Defense": {"sum": 0.0, "count": 0},
        }
    )

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            map_name = (map_entry.get("map_name", "") or "").strip()
            if not map_name:
                continue

            if map_name not in map_timeline_targets and scrim.get("id") is not None:
                map_timeline_targets[map_name] = scrim.get("id")

            mode_name = MAP_MODES.get(map_name, "Other")
            result = map_entry.get("result")

            map_records[map_name]["maps"] += 1
            mode_records[mode_name]["maps"] += 1

            if result == "Win":
                map_records[map_name]["wins"] += 1
                mode_records[mode_name]["wins"] += 1
            elif result == "Loss":
                map_records[map_name]["losses"] += 1
                mode_records[mode_name]["losses"] += 1

            for section in map_entry.get("comp", []):
                section_side = (section.get("side") or "").strip()
                if section_side not in ("Attack", "Defense"):
                    continue
                numeric_score = score_for_perspective(section.get("score", ""), perspective="left")
                if numeric_score is None:
                    continue
                side_score_records[map_name][section_side]["sum"] += numeric_score
                side_score_records[map_name][section_side]["count"] += 1

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
                "attack_score_avg": (
                    round(
                        side_score_records[map_name]["Attack"]["sum"]
                        / side_score_records[map_name]["Attack"]["count"],
                        2,
                    )
                    if side_score_records[map_name]["Attack"]["count"]
                    else None
                ),
                "defense_score_avg": (
                    round(
                        side_score_records[map_name]["Defense"]["sum"]
                        / side_score_records[map_name]["Defense"]["count"],
                        2,
                    )
                    if side_score_records[map_name]["Defense"]["count"]
                    else None
                ),
            }
        )
    team_map_cards.sort(key=lambda r: (r["win_rate"], r["maps"]), reverse=True)

    team_map_mode_rows = []
    for mode_name, stats in mode_records.items():
        maps_played = stats["maps"]
        win_rate = round((stats["wins"] / maps_played) * 100, 1) if maps_played else 0
        mode_maps = [card for card in team_map_cards if card["mode"] == mode_name]
        best_map = max(mode_maps, key=lambda m: (m["win_rate"], m["maps"]), default=None)
        worst_map = min(mode_maps, key=lambda m: (m["win_rate"], -m["maps"]), default=None)
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
    team_map_mode_rows.sort(key=lambda r: (r["win_rate"], r["maps"]), reverse=True)

    best_mode = team_map_mode_rows[0] if team_map_mode_rows else None
    worst_mode = team_map_mode_rows[-1] if team_map_mode_rows else None
    return team_map_cards, team_map_mode_rows, best_mode, worst_mode


def _hero_match_key(hero_name: str) -> str:
    resolved = _resolve_hero_transform_key(hero_name)
    return _compact_text(resolved or hero_name)


def _hero_role(hero_name: str) -> str:
    key = _hero_match_key(hero_name)
    if not key:
        return ""
    for role_name, heroes in HERO_ROLES.items():
        for hero in heroes:
            if _compact_text(hero) == key:
                return role_name
    return ""


def _canonical_section_hero_instances(section: dict, team_slot: str) -> list[str]:
    lineup = section.get(team_slot, []) if isinstance(section, dict) else []
    if not isinstance(lineup, list):
        return []

    instances: list[str] = []
    for slot in lineup:
        if not isinstance(slot, dict):
            continue
        hero_name = _canonical_draft_hero(slot.get("hero", ""))
        if hero_name:
            instances.append(hero_name)
    return instances


def _canonical_map_hero_instances(map_entry: dict, team_slot: str) -> list[str]:
    instances: list[str] = []
    for section in map_entry.get("comp", []):
        instances.extend(_canonical_section_hero_instances(section, team_slot))
    return instances


def build_team_hero_insights(team_scrims: list[dict], hero_name: str) -> dict:
    target_name = (hero_name or "").strip()
    target_key = _hero_match_key(target_name)
    display_name = _resolve_hero_transform_key(target_name) or target_name
    target_role = _hero_role(display_name)

    ally_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    duo_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    comp_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    map_stats = defaultdict(lambda: {"maps": 0, "wins": 0, "losses": 0})

    total_maps = 0
    total_wins = 0
    total_losses = 0
    total_instances = 0
    timeline_points = []
    map_log_rows = []
    hero_map_mode_observations = []

    ban_tracked_maps = 0
    banned_maps = 0
    open_maps = 0
    banned_wins = 0
    banned_losses = 0
    open_wins = 0
    open_losses = 0
    ban_pivot_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})

    sorted_scrims = sorted(team_scrims, key=lambda s: (s.get("scrim_date", ""), s.get("id", 0)))

    for scrim in sorted_scrims:
        scrim_maps = 0
        scrim_wins = 0
        scrim_losses = 0

        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            result = get_map_outcome_for_slot(map_entry, our_team_slot)

            enemy_team_slot = "team2" if our_team_slot == "team1" else "team1"
            draft = map_entry.get("draft") if isinstance(map_entry.get("draft"), dict) else {}
            enemy_draft = draft.get(enemy_team_slot) if isinstance(draft.get(enemy_team_slot), dict) else {}
            enemy_ban_keys = ["ban1", "ban2", "ban3", "ban4"]
            enemy_bans = [
                _canonical_draft_hero(enemy_draft.get(k, ""))
                for k in enemy_ban_keys
                if _canonical_draft_hero(enemy_draft.get(k, ""))
            ]
            has_enemy_ban_data = bool(enemy_bans)
            target_banned = any(_hero_match_key(b) == target_key for b in enemy_bans)
            if has_enemy_ban_data:
                ban_tracked_maps += 1
                if target_banned:
                    banned_maps += 1
                    if result == "Win":
                        banned_wins += 1
                    elif result == "Loss":
                        banned_losses += 1
                else:
                    open_maps += 1
                    if result == "Win":
                        open_wins += 1
                    elif result == "Loss":
                        open_losses += 1

            map_has_hero = False
            map_instances = 0
            for section in map_entry.get("comp", []):
                our_heroes = _canonical_section_hero_instances(section, our_team_slot)
                if not our_heroes:
                    continue

                target_instances = sum(1 for hero in our_heroes if _hero_match_key(hero) == target_key)
                if not target_instances:
                    continue

                map_has_hero = True
                map_instances += target_instances

                teammates = [hero for hero in our_heroes if _hero_match_key(hero) != target_key]
                for teammate in teammates:
                    ally_stats[teammate]["count"] += 1
                    if result == "Win":
                        ally_stats[teammate]["wins"] += 1
                    elif result == "Loss":
                        ally_stats[teammate]["losses"] += 1

                if target_role:
                    same_role_partners = [
                        hero
                        for hero in our_heroes
                        if _hero_match_key(hero) != target_key and _hero_role(hero) == target_role
                    ]
                    for duo_partner in same_role_partners:
                        duo_stats[duo_partner]["count"] += 1
                        if result == "Win":
                            duo_stats[duo_partner]["wins"] += 1
                        elif result == "Loss":
                            duo_stats[duo_partner]["losses"] += 1

                comp_signature = tuple(sorted(our_heroes))
                if comp_signature:
                    comp_stats[comp_signature]["count"] += 1
                    if result == "Win":
                        comp_stats[comp_signature]["wins"] += 1
                    elif result == "Loss":
                        comp_stats[comp_signature]["losses"] += 1

                if target_banned:
                    for pivot_hero in teammates:
                        ban_pivot_stats[pivot_hero]["count"] += 1
                        if result == "Win":
                            ban_pivot_stats[pivot_hero]["wins"] += 1
                        elif result == "Loss":
                            ban_pivot_stats[pivot_hero]["losses"] += 1

            if not map_has_hero:
                continue

            map_name = (map_entry.get("map_name") or "").strip()
            source_label = (
                str(scrim.get("source") or scrim.get("event_type") or scrim.get("event") or scrim.get("tournament_name") or "")
                .strip()
                .lower()
                or "scrim"
            )
            if result in {"Win", "Loss"}:
                hero_map_mode_observations.append(
                    {
                        "win": 1.0 if result == "Win" else 0.0,
                        "mode": MAP_MODES.get(map_name, "Other") if map_name else "Other",
                        "map": map_name or "Unknown Map",
                        "source": source_label,
                        "side": our_team_slot,
                    }
                )
            if map_name:
                map_stats[map_name]["maps"] += 1

            map_log_rows.append(
                {
                    "scrim_id": scrim.get("id"),
                    "map_id": map_entry.get("id"),
                    "scrim_date": (scrim.get("scrim_date") or "").strip(),
                    "opponent_name": (scrim.get("enemy_team") or scrim.get("opponent") or "Unknown").strip() or "Unknown",
                    "map_name": map_name or "Unknown Map",
                    "result": result or "Not Set",
                    "instances": map_instances,
                }
            )

            total_maps += 1
            total_instances += map_instances
            scrim_maps += 1
            if result == "Win":
                total_wins += 1
                scrim_wins += 1
                if map_name:
                    map_stats[map_name]["wins"] += 1
            elif result == "Loss":
                total_losses += 1
                scrim_losses += 1
                if map_name:
                    map_stats[map_name]["losses"] += 1

        if scrim_maps:
            label = f"{scrim.get('scrim_date', '')} vs {scrim.get('enemy_team') or scrim.get('opponent') or 'Unknown'}"
            timeline_points.append(
                {
                    "label": label,
                    "maps": scrim_maps,
                    "wins": scrim_wins,
                    "losses": scrim_losses,
                    "scrim_win_rate": round((scrim_wins / scrim_maps) * 100, 1) if scrim_maps else 0,
                    "cumulative_win_rate": round((total_wins / total_maps) * 100, 1) if total_maps else 0,
                }
            )

    ally_rows = []
    for ally_name, stats in ally_stats.items():
        count = stats["count"]
        ally_rows.append(
            {
                "hero": ally_name,
                "count": count,
                "win_rate": round((stats["wins"] / count) * 100, 1) if count else 0,
            }
        )
    ally_rows.sort(key=lambda r: (r["count"], r["win_rate"]), reverse=True)

    duo_rows = []
    for duo_name, stats in duo_stats.items():
        count = stats["count"]
        duo_rows.append(
            {
                "hero": duo_name,
                "count": count,
                "win_rate": round((stats["wins"] / count) * 100, 1) if count else 0,
            }
        )
    duo_rows.sort(key=lambda r: (r["count"], r["win_rate"]), reverse=True)

    comp_rows = []
    for comp_signature, stats in comp_stats.items():
        count = stats["count"]
        comp_rows.append(
            {
                "heroes": list(comp_signature),
                "count": count,
                "win_rate": round((stats["wins"] / count) * 100, 1) if count else 0,
            }
        )
    comp_rows.sort(key=lambda r: (r["count"], r["win_rate"]), reverse=True)

    map_rows = []
    for map_name, stats in map_stats.items():
        maps_played = stats["maps"]
        map_rows.append(
            {
                "map_name": map_name,
                "mode": MAP_MODES.get(map_name, "Unknown"),
                "maps": maps_played,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round((stats["wins"] / maps_played) * 100, 1) if maps_played else 0,
                "image": get_map_image_url(map_name),
            }
        )
    map_rows.sort(key=lambda r: (r["maps"], r["win_rate"]), reverse=True)

    def build_hero_map_mode_anova(observations: list[dict]) -> dict:
        try:
            import numpy as np
        except Exception:
            return {}

        if len(observations) < 4:
            return {
                "status": "insufficient",
                "significance": "insufficient sample",
                "sample": len(observations),
            }

        def pct(part: int, whole: int) -> float:
            return round((part / whole) * 100, 1) if whole else 0.0

        def build_matrix(items: list[dict], factors: tuple[str, ...]):
            columns = [[1.0] * len(items)]
            for factor in factors:
                values = sorted({str(row.get(factor) or "") for row in items})
                if len(values) <= 1:
                    continue
                for value in values[1:]:
                    columns.append([1.0 if str(row.get(factor) or "") == value else 0.0 for row in items])
            return np.asarray(columns, dtype=float).T

        def sse_for(matrix, y_values) -> tuple[float, int]:
            beta, *_ = np.linalg.lstsq(matrix, y_values, rcond=None)
            residuals = y_values - matrix.dot(beta)
            rank = int(np.linalg.matrix_rank(matrix))
            return float(np.sum(residuals ** 2)), max(0, len(y_values) - rank)

        mode_rows = []
        for mode_name in sorted({row.get("mode") or "Other" for row in observations}):
            mode_items = [row for row in observations if (row.get("mode") or "Other") == mode_name]
            wins = sum(1 for row in mode_items if row["win"] >= 1.0)
            mode_rows.append({"mode": mode_name, "maps": len(mode_items), "win_rate": pct(wins, len(mode_items))})
        mode_rows.sort(key=lambda row: (row["maps"], row["win_rate"]), reverse=True)

        map_anova_rows = []
        for map_name_value in sorted({row.get("map") or "Unknown Map" for row in observations}):
            map_items = [row for row in observations if (row.get("map") or "Unknown Map") == map_name_value]
            wins = sum(1 for row in map_items if row["win"] >= 1.0)
            map_anova_rows.append({"map": map_name_value, "maps": len(map_items), "win_rate": pct(wins, len(map_items))})
        map_anova_rows.sort(key=lambda row: (row["maps"], row["win_rate"]), reverse=True)

        if len(mode_rows) <= 1 and len(map_anova_rows) <= 1:
            return {
                "status": "insufficient",
                "significance": "single map/mode",
                "sample": len(observations),
                "mode_rows": mode_rows[:4],
                "map_rows": map_anova_rows[:4],
            }

        try:
            y = np.asarray([row["win"] for row in observations], dtype=float)
            reduced = build_matrix(observations, ("source", "side"))
            full = build_matrix(observations, ("source", "side", "mode", "map"))
            sse_reduced, df_reduced = sse_for(reduced, y)
            sse_full, df_full = sse_for(full, y)
        except Exception:
            return {}

        df_num = max(1, df_reduced - df_full)
        if df_full <= 0 or sse_full <= 0 or sse_reduced < sse_full:
            f_stat = 0.0
            partial_eta = 0.0
        else:
            ss_effect = max(0.0, sse_reduced - sse_full)
            f_stat = (ss_effect / df_num) / (sse_full / df_full)
            partial_eta = ss_effect / (ss_effect + sse_full) if (ss_effect + sse_full) else 0.0

        best_map = max(map_anova_rows, key=lambda row: (row["win_rate"], row["maps"])) if map_anova_rows else None
        worst_map = min(map_anova_rows, key=lambda row: (row["win_rate"], -row["maps"])) if map_anova_rows else None
        best_mode = max(mode_rows, key=lambda row: (row["win_rate"], row["maps"])) if mode_rows else None
        worst_mode = min(mode_rows, key=lambda row: (row["win_rate"], -row["maps"])) if mode_rows else None
        map_spread = round((best_map["win_rate"] - worst_map["win_rate"]), 1) if best_map and worst_map else 0.0
        mode_spread = round((best_mode["win_rate"] - worst_mode["win_rate"]), 1) if best_mode and worst_mode else 0.0

        if len(observations) < 6:
            significance = "low sample"
        elif f_stat >= 6.8 and partial_eta >= 0.10:
            significance = "strong"
        elif f_stat >= 3.8 and partial_eta >= 0.05:
            significance = "moderate"
        elif f_stat >= 2.0 and partial_eta >= 0.025:
            significance = "weak"
        else:
            significance = "not significant"

        return {
            "status": "ready",
            "significance": significance,
            "sample": len(observations),
            "win_rate": pct(int(sum(row["win"] for row in observations)), len(observations)),
            "f_stat": round(f_stat, 3),
            "df_num": df_num,
            "df_den": df_full,
            "partial_eta_sq": round(partial_eta, 4),
            "map_spread": map_spread,
            "mode_spread": mode_spread,
            "best_map": best_map,
            "worst_map": worst_map,
            "best_mode": best_mode,
            "worst_mode": worst_mode,
            "mode_rows": mode_rows[:4],
            "map_rows": map_anova_rows[:4],
            "factors": ["map_mode", "map_name", "source", "side"],
        }

    map_mode_anova = build_hero_map_mode_anova(hero_map_mode_observations)
    map_log_rows.sort(
        key=lambda row: (
            row.get("scrim_date", ""),
            int(row.get("scrim_id") or 0),
            int(row.get("map_id") or 0),
        ),
        reverse=True,
    )

    open_decisions = open_wins + open_losses
    banned_decisions = banned_wins + banned_losses
    open_wr = round((open_wins / open_decisions) * 100, 1) if open_decisions else None
    banned_wr = round((banned_wins / banned_decisions) * 100, 1) if banned_decisions else None
    open_vs_banned_delta = round(open_wr - banned_wr, 1) if (open_wr is not None and banned_wr is not None) else None

    ban_pivot_rows = []
    for pivot_hero, stats in ban_pivot_stats.items():
        count = stats["count"]
        if not count:
            continue
        decisions = stats["wins"] + stats["losses"]
        ban_pivot_rows.append(
            {
                "hero": pivot_hero,
                "count": count,
                "win_rate": round((stats["wins"] / decisions) * 100, 1) if decisions else None,
            }
        )
    ban_pivot_rows.sort(key=lambda row: (row["count"], row["win_rate"] or 0), reverse=True)

    return {
        "hero": display_name,
        "target_role": target_role,
        "hero_image_url": _hero_image_url(display_name),
        "summary": {
            "maps_played": total_maps,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate": round((total_wins / total_maps) * 100, 1) if total_maps else 0,
            "hero_instances": total_instances,
            "unique_allies": len(ally_rows),
        },
        "ally_rows": ally_rows,
        "duo_rows": duo_rows,
        "comp_rows": comp_rows,
        "map_rows": map_rows,
        "map_mode_anova": map_mode_anova,
        "map_log_rows": map_log_rows,
        "timeline_points": timeline_points,
        "ban_impact": {
            "tracked_maps": ban_tracked_maps,
            "banned_maps": banned_maps,
            "open_maps": open_maps,
            "banned_rate": round((banned_maps / ban_tracked_maps) * 100, 1) if ban_tracked_maps else 0,
            "win_rate_when_open": open_wr,
            "win_rate_when_banned": banned_wr,
            "open_vs_banned_delta": open_vs_banned_delta,
            "open_decisions": open_decisions,
            "banned_decisions": banned_decisions,
            "top_pivots": ban_pivot_rows[:5],
        },
    }


