# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def build_matchup_tree_model(
    team_a_name: str,
    team_a_scrims: list[dict],
    team_b_name: str,
    team_b_scrims: list[dict],
    team_a_hero_pool_scrims: list[dict] | None = None,
    team_b_hero_pool_scrims: list[dict] | None = None,
) -> dict:
    team_a_model = build_opponent_tree_model(team_a_scrims, hero_pool_scrims=team_a_hero_pool_scrims)
    team_b_model = build_opponent_tree_model(team_b_scrims, hero_pool_scrims=team_b_hero_pool_scrims)

    if (
        team_a_model.get("status") == "empty"
        and team_b_model.get("status") == "empty"
    ):
        return {
            "status": "empty",
            "teams": [
                {"name": team_a_name, "model": team_a_model},
                {"name": team_b_name, "model": team_b_model},
            ],
            "contested_heroes": [],
            "force_matchup_rows": [],
            "deviation_rows": [],
            "volatile_matchup_rows": [],
            "pivot_pressure_rows": [],
            "map_effect_rows": [],
            "equivalent_path_rows": [],
            "ban_clash_rows": [],
            "comp_clash_rows": [],
            "map_consensus_rows": [],
        }

    def hero_index(rows: list[dict]) -> dict[str, dict]:
        indexed: dict[str, dict] = {}
        for row in rows:
            hero_name = (row.get("hero", "") or "").strip()
            if hero_name:
                indexed[hero_name] = row
        return indexed

    def line_pair_score(line_a: dict, line_b: dict, overlap_count: int) -> tuple:
        return (
            overlap_count,
            round((float(line_a.get("rate", 0) or 0) + float(line_b.get("rate", 0) or 0)) / 2.0, 2),
            round((float(line_a.get("line_strength", 0) or 0) + float(line_b.get("line_strength", 0) or 0)) / 2.0, 2),
        )

    def comp_pair_score(comp_a: dict, comp_b: dict, overlap_count: int) -> tuple:
        return (
            overlap_count,
            round((float(comp_a.get("rate", 0) or 0) + float(comp_b.get("rate", 0) or 0)) / 2.0, 2),
            round((float(comp_a.get("comp_strength", 0) or 0) + float(comp_b.get("comp_strength", 0) or 0)) / 2.0, 2),
        )

    def conditioned_enemy_comp_rows(enemy_comp_paths: list[dict], our_path: dict) -> list[dict]:
        our_bans = set(our_path.get("bans", []))
        scored_rows = []
        for comp_row in enemy_comp_paths:
            heroes = [hero for hero in comp_row.get("heroes", []) if hero]
            blocked = len(our_bans & set(heroes))
            hero_count = len(heroes) or 1
            preserved_ratio = max(0.0, (hero_count - blocked) / hero_count)
            conditioned_rate = round(float(comp_row.get("top_path_share", 0) or 0) * preserved_ratio, 1)
            scored_rows.append(
                {
                    "heroes": heroes,
                    "blocked_heroes": sorted(our_bans & set(heroes)),
                    "blocked_count": blocked,
                    "preserved_ratio": round(preserved_ratio * 100, 1),
                    "conditioned_rate": conditioned_rate,
                    "top_path_share": float(comp_row.get("top_path_share", 0) or 0),
                    "path_count": int(comp_row.get("path_count", 0) or 0),
                }
            )
        scored_rows.sort(
            key=lambda row: (row["conditioned_rate"], -row["blocked_count"], row["top_path_share"]),
            reverse=True,
        )
        return scored_rows

    def build_player_pressure_rows(enemy_model: dict) -> list[dict]:
        player_rows = enemy_model.get("player_hero_rows", [])
        hero_pressure: dict[str, dict] = {}

        def hero_score(hero_row: dict) -> float:
            appearances = float(hero_row.get("appearances", 0) or 0)
            usage_rate = float(hero_row.get("usage_rate", 0) or 0)
            win_rate = float(hero_row.get("win_rate", 0) or 0)
            return (appearances * 2.5) + (usage_rate * 0.8) + ((win_rate - 50.0) * 0.18)

        for player_row in player_rows:
            player_name = (player_row.get("player", "") or "").strip()
            if not player_name:
                continue

            hero_rows = [
                row for row in (player_row.get("top_heroes", []) or [])
                if (row.get("hero") or "").strip() and float(row.get("appearances", 0) or 0) > 0
            ]
            if len(hero_rows) < 2:
                continue

            # Use most-played fallback first, then usage and win rate as tie-breakers.
            sorted_rows = sorted(
                hero_rows,
                key=lambda row: (
                    float(row.get("appearances", 0) or 0),
                    float(row.get("usage_rate", 0) or 0),
                    float(row.get("win_rate", 0) or 0),
                ),
                reverse=True,
            )
            primary = sorted_rows[0]
            fallback = sorted_rows[1]
            primary_hero = (primary.get("hero", "") or "").strip()
            fallback_hero = (fallback.get("hero", "") or "").strip()
            if not primary_hero or not fallback_hero:
                continue

            score_drop = max(0.0, hero_score(primary) - hero_score(fallback))
            if score_drop <= 0:
                continue

            bucket = hero_pressure.setdefault(
                primary_hero,
                {
                    "hero": primary_hero,
                    "affected_players": [],
                    "affected_count": 0,
                    "total_score_drop": 0.0,
                    "max_score_drop": 0.0,
                },
            )
            bucket["affected_players"].append(
                {
                    "player": player_name,
                    "fallback_hero": fallback_hero,
                    "score_drop": round(score_drop, 1),
                    "primary_appearances": int(float(primary.get("appearances", 0) or 0)),
                }
            )
            bucket["affected_count"] += 1
            bucket["total_score_drop"] += score_drop
            bucket["max_score_drop"] = max(float(bucket["max_score_drop"]), score_drop)

        rows = []
        for row in hero_pressure.values():
            affected_players = sorted(
                row["affected_players"],
                key=lambda p: (float(p.get("score_drop", 0) or 0), int(p.get("primary_appearances", 0) or 0)),
                reverse=True,
            )
            rows.append(
                {
                    "hero": row["hero"],
                    "affected_players": affected_players,
                    "affected_count": int(row["affected_count"]),
                    "total_score_drop": round(float(row["total_score_drop"]), 1),
                    "max_score_drop": round(float(row["max_score_drop"]), 1),
                }
            )

        rows.sort(
            key=lambda row: (
                int(row.get("affected_count", 0) or 0),
                float(row.get("total_score_drop", 0) or 0),
                float(row.get("max_score_drop", 0) or 0),
                (row.get("hero", "") or "").lower(),
            ),
            reverse=True,
        )
        return rows

    team_a_heroes = hero_index(team_a_model.get("comfort_core_rows", []))
    team_b_heroes = hero_index(team_b_model.get("comfort_core_rows", []))

    contested_heroes = []
    for hero_name in sorted(set(team_a_heroes) & set(team_b_heroes)):
        hero_a = team_a_heroes[hero_name]
        hero_b = team_b_heroes[hero_name]
        contested_heroes.append(
            {
                "hero": hero_name,
                "team_a_score": float(hero_a.get("profile_score", 0) or 0),
                "team_b_score": float(hero_b.get("profile_score", 0) or 0),
                "team_a_comfort": float(hero_a.get("comfort_rate", 0) or 0),
                "team_b_comfort": float(hero_b.get("comfort_rate", 0) or 0),
                "team_a_adj_wr": float(hero_a.get("adjusted_win_rate", 0) or 0),
                "team_b_adj_wr": float(hero_b.get("adjusted_win_rate", 0) or 0),
                "contested_score": round(
                    (
                        float(hero_a.get("profile_score", 0) or 0)
                        + float(hero_b.get("profile_score", 0) or 0)
                    ) / 2.0,
                    1,
                ),
            }
        )
    contested_heroes.sort(
        key=lambda row: (
            row["contested_score"],
            row["team_a_comfort"] + row["team_b_comfort"],
            row["hero"].lower(),
        ),
        reverse=True,
    )

    ban_clash_rows = []
    team_a_lines = team_a_model.get("ban_line_rows", [])[:6]
    team_b_lines = team_b_model.get("ban_line_rows", [])[:6]
    for line_a in team_a_lines:
        heroes_a = [hero for hero in line_a.get("heroes", []) if hero]
        hero_set_a = set(heroes_a)
        for line_b in team_b_lines:
            heroes_b = [hero for hero in line_b.get("heroes", []) if hero]
            overlap = sorted(hero_set_a & set(heroes_b))
            score_tuple = line_pair_score(line_a, line_b, len(overlap))
            ban_clash_rows.append(
                {
                    "team_a_heroes": heroes_a,
                    "team_b_heroes": heroes_b,
                    "shared_heroes": overlap,
                    "shared_count": len(overlap),
                    "combined_rate": score_tuple[1],
                    "combined_strength": score_tuple[2],
                }
            )
    ban_clash_rows.sort(
        key=lambda row: (
            row["shared_count"],
            row["combined_rate"],
            row["combined_strength"],
        ),
        reverse=True,
    )

    comp_clash_rows = []
    team_a_comps = team_a_model.get("comp_rows", [])[:6]
    team_b_comps = team_b_model.get("comp_rows", [])[:6]
    for comp_a in team_a_comps:
        heroes_a = [hero for hero in comp_a.get("heroes", []) if hero]
        hero_set_a = set(heroes_a)
        for comp_b in team_b_comps:
            heroes_b = [hero for hero in comp_b.get("heroes", []) if hero]
            overlap = sorted(hero_set_a & set(heroes_b))
            score_tuple = comp_pair_score(comp_a, comp_b, len(overlap))
            comp_clash_rows.append(
                {
                    "team_a_heroes": heroes_a,
                    "team_b_heroes": heroes_b,
                    "shared_heroes": overlap,
                    "shared_count": len(overlap),
                    "combined_rate": score_tuple[1],
                    "combined_strength": score_tuple[2],
                }
            )
    comp_clash_rows.sort(
        key=lambda row: (
            row["shared_count"],
            row["combined_rate"],
            row["combined_strength"],
        ),
        reverse=True,
    )

    state_lookup_a = {
        row.get("label", ""): row
        for row in team_a_model.get("map_state_rows", [])
        if row.get("label")
    }
    state_lookup_b = {
        row.get("label", ""): row
        for row in team_b_model.get("map_state_rows", [])
        if row.get("label")
    }
    map_consensus_rows = []
    for state_label in sorted(set(state_lookup_a) & set(state_lookup_b)):
        options_a = {
            option.get("map_name", ""): option
            for option in state_lookup_a[state_label].get("options", [])
            if option.get("map_name")
        }
        options_b = {
            option.get("map_name", ""): option
            for option in state_lookup_b[state_label].get("options", [])
            if option.get("map_name")
        }
        shared_maps = []
        for map_name in sorted(set(options_a) & set(options_b)):
            map_a = options_a[map_name]
            map_b = options_b[map_name]
            shared_maps.append(
                {
                    "map_name": map_name,
                    "image": get_map_image_url(map_name),
                    "mode": map_a.get("mode", "Other"),
                    "team_a_rate": float(map_a.get("rate", 0) or 0),
                    "team_b_rate": float(map_b.get("rate", 0) or 0),
                    "combined_rate": round(
                        (
                            float(map_a.get("rate", 0) or 0)
                            + float(map_b.get("rate", 0) or 0)
                        ) / 2.0,
                        1,
                    ),
                }
            )
        shared_maps.sort(
            key=lambda row: (row["combined_rate"], row["map_name"].lower()),
            reverse=True,
        )
        if shared_maps:
            map_consensus_rows.append(
                {
                    "label": state_label,
                    "options": shared_maps[:6],
                }
            )

    comp_ml_lookup_a = {
        tuple(row.get("heroes", [])): {
            "ml_win_prob": float(row.get("ml_win_prob", row.get("comp_win_rate", 0)) or 0),
            "ml_confidence": float(row.get("ml_confidence", 0) or 0),
            "ml_comp_score": float(row.get("ml_comp_score", row.get("comp_strength", 0)) or 0),
        }
        for row in team_a_model.get("comp_rows", [])
        if row.get("heroes")
    }

    force_matchup_rows = []
    team_a_comp_paths = team_a_model.get("comp_path_rows", [])[:6]
    team_b_comp_paths = team_b_model.get("comp_path_rows", [])[:6]
    for our_comp in team_a_comp_paths:
        our_paths = our_comp.get("top_draft_paths", [])
        if not our_paths:
            continue
        primary_path = our_paths[0]
        enemy_options = conditioned_enemy_comp_rows(team_b_comp_paths, primary_path)
        if not enemy_options:
            continue
        top_enemy = enemy_options[0]
        second_enemy = enemy_options[1] if len(enemy_options) > 1 else None
        force_matchup_rows.append(
            {
                "our_comp": our_comp.get("heroes", []),
                "enemy_comp": top_enemy["heroes"],
                "our_bans": primary_path.get("bans", []),
                "our_protects": primary_path.get("protects", []),
                "our_path_share": float(primary_path.get("rate", 0) or 0),
                "our_deviation_budget": our_comp.get("deviation_budget", 0),
                "enemy_blocked_count": top_enemy["blocked_count"],
                "enemy_blocked_heroes": top_enemy["blocked_heroes"],
                "enemy_choice_gap": round(
                    top_enemy["conditioned_rate"] - float(second_enemy["conditioned_rate"]) if second_enemy else top_enemy["conditioned_rate"],
                    1,
                ),
                "enemy_alt_comp": second_enemy["heroes"] if second_enemy else [],
                "enemy_conditioned_rate": top_enemy["conditioned_rate"],
                "enemy_preserved_ratio": top_enemy["preserved_ratio"],
                "micro_path_count": int(our_comp.get("path_count", 0) or 0),
                "ml_win_prob": comp_ml_lookup_a.get(tuple(our_comp.get("heroes", [])), {}).get("ml_win_prob", 0.0),
                "ml_confidence": comp_ml_lookup_a.get(tuple(our_comp.get("heroes", [])), {}).get("ml_confidence", 0.0),
                "ml_comp_score": comp_ml_lookup_a.get(tuple(our_comp.get("heroes", [])), {}).get("ml_comp_score", 0.0),
            }
        )
    force_matchup_rows.sort(
        key=lambda row: (
            ((float(row.get("ml_comp_score", 0) or 0) * 0.55)
             + (float(row.get("our_path_share", 0) or 0) * 0.25)
             + (float(row.get("enemy_choice_gap", 0) or 0) * 0.20)),
            row["our_path_share"],
            row["enemy_choice_gap"],
            -row["enemy_blocked_count"],
        ),
        reverse=True,
    )

    team_b_pressure_rows = build_player_pressure_rows(team_b_model)

    ml_outlook_rows = []
    for row in force_matchup_rows[:6]:
        our_comp = row.get("our_comp", [])
        our_bans = row.get("our_bans", [])
        enemy_comp = row.get("enemy_comp", [])
        pressure_bans = [
            {
                "hero": pressure_row.get("hero", ""),
                "affected_count": int(pressure_row.get("affected_count", 0) or 0),
                "total_score_drop": round(float(pressure_row.get("total_score_drop", 0) or 0), 1),
            }
            for pressure_row in team_b_pressure_rows
            if (pressure_row.get("hero", "") or "")
            and (pressure_row.get("hero", "") not in set(our_comp))
        ][:3]

        top_pressure_drop = float(pressure_bans[0].get("total_score_drop", 0) or 0) if pressure_bans else 0.0
        top_pressure_count = int(pressure_bans[0].get("affected_count", 0) or 0) if pressure_bans else 0
        enemy_preserved = float(row.get("enemy_preserved_ratio", 0) or 0)
        enemy_choice_gap = float(row.get("enemy_choice_gap", 0) or 0)
        ml_win_prob = float(row.get("ml_win_prob", 0) or 0)
        ml_confidence = float(row.get("ml_confidence", 0) or 0)
        ml_comp_score = float(row.get("ml_comp_score", 0) or 0)
        our_path_share = float(row.get("our_path_share", 0) or 0)
        blocked_count = int(row.get("enemy_blocked_count", 0) or 0)
        path_ban_set = set(our_bans)

        pressure_bans = [
            pressure for pressure in pressure_bans
            if (pressure.get("hero", "") or "") not in path_ban_set
        ]

        read_score = (
            (ml_win_prob * 0.28)
            + (ml_confidence * 0.16)
            + (ml_comp_score * 0.20)
            + ((100.0 - enemy_preserved) * 0.14)
            + (enemy_choice_gap * 0.10)
            + (our_path_share * 0.05)
            + (blocked_count * 2.2)
            + (top_pressure_drop * 0.10)
            + (top_pressure_count * 1.2)
        )
        if read_score >= 68:
            read_tier = "High Leverage"
        elif read_score >= 54:
            read_tier = "Strong"
        elif read_score >= 42:
            read_tier = "Playable"
        else:
            read_tier = "Volatile"

        ml_outlook_rows.append(
            {
                "our_comp": our_comp,
                "our_bans": our_bans,
                "our_protects": row.get("our_protects", []),
                "enemy_comp": enemy_comp,
                "enemy_alt_comp": row.get("enemy_alt_comp", []),
                "ml_win_prob": round(float(row.get("ml_win_prob", 0) or 0), 1),
                "ml_confidence": round(float(row.get("ml_confidence", 0) or 0), 1),
                "ml_comp_score": round(ml_comp_score, 1),
                "enemy_choice_gap": round(enemy_choice_gap, 1),
                "our_path_share": round(our_path_share, 1),
                "enemy_blocked_count": blocked_count,
                "enemy_blocked_heroes": row.get("enemy_blocked_heroes", []),
                "enemy_conditioned_rate": round(float(row.get("enemy_conditioned_rate", 0) or 0), 1),
                "enemy_preserved_ratio": round(enemy_preserved, 1),
                "pressure_bans": pressure_bans,
                "top_pressure_drop": round(top_pressure_drop, 1),
                "top_pressure_count": top_pressure_count,
                "read_score": round(read_score, 1),
                "read_tier": read_tier,
            }
        )

    ml_outlook_rows.sort(
        key=lambda out: (
            float(out.get("read_score", 0) or 0),
            float(out.get("ml_win_prob", 0) or 0),
            float(out.get("our_path_share", 0) or 0),
        ),
        reverse=True,
    )

    if ml_outlook_rows:
        score_values = sorted(
            [float(row.get("read_score", 0) or 0) for row in ml_outlook_rows],
            reverse=True,
        )

        def score_percentile(p: float) -> float:
            if not score_values:
                return 0.0
            index = int(round((len(score_values) - 1) * p))
            index = max(0, min(len(score_values) - 1, index))
            return float(score_values[index])

        high_cut = max(76.0, score_percentile(0.25))
        strong_cut = max(62.0, score_percentile(0.50))
        playable_cut = max(50.0, score_percentile(0.75))

        for outlook in ml_outlook_rows:
            score = float(outlook.get("read_score", 0) or 0)
            if score >= high_cut:
                tier = "High Leverage"
            elif score >= strong_cut:
                tier = "Strong"
            elif score >= playable_cut:
                tier = "Playable"
            else:
                tier = "Volatile"
            outlook["read_tier"] = tier

    deviation_rows = []
    for our_comp in team_a_comp_paths[:6]:
        draft_paths = our_comp.get("top_draft_paths", [])
        if len(draft_paths) < 2:
            continue
        primary_path = draft_paths[0]
        alt_path = draft_paths[1]
        primary_enemy = conditioned_enemy_comp_rows(team_b_comp_paths, primary_path)
        alt_enemy = conditioned_enemy_comp_rows(team_b_comp_paths, alt_path)
        primary_enemy_comp = primary_enemy[0]["heroes"] if primary_enemy else []
        alt_enemy_comp = alt_enemy[0]["heroes"] if alt_enemy else []
        deviation_rows.append(
            {
                "our_comp": our_comp.get("heroes", []),
                "primary_bans": primary_path.get("bans", []),
                "alt_bans": alt_path.get("bans", []),
                "path_distance": alt_path.get("distance_from_primary", 0),
                "stable_slots": our_comp.get("stable_slots", 0),
                "deviation_budget": our_comp.get("deviation_budget", 0),
                "enemy_primary_comp": primary_enemy_comp,
                "enemy_alt_comp": alt_enemy_comp,
                "enemy_response_changed": primary_enemy_comp != alt_enemy_comp,
                "enemy_response_shift": round(
                    (float(primary_enemy[0]["conditioned_rate"]) - float(alt_enemy[0]["conditioned_rate"]))
                    if primary_enemy and alt_enemy
                    else 0.0,
                    1,
                ),
            }
        )
    deviation_rows.sort(
        key=lambda row: (row["deviation_budget"], row["path_distance"], row["enemy_response_changed"]),
        reverse=True,
    )

    volatile_matchup_rows = []
    volatile_lookup_a = {row["hero"]: row for row in team_a_model.get("volatile_hero_rows", [])}
    volatile_lookup_b = {row["hero"]: row for row in team_b_model.get("volatile_hero_rows", [])}
    # Only consider heroes with meaningful appearances in at least one team.
    hero_pool_a = {row["hero"] for row in team_a_model.get("hero_pool_rows", [])}
    hero_pool_b = {row["hero"] for row in team_b_model.get("hero_pool_rows", [])}
    relevant_volatile_heroes = (set(volatile_lookup_a) | set(volatile_lookup_b)) & (hero_pool_a | hero_pool_b)
    for hero_name in sorted(relevant_volatile_heroes):
        row_a = volatile_lookup_a.get(hero_name, {})
        row_b = volatile_lookup_b.get(hero_name, {})
        delta_a = float(row_a.get("delta", 0) or 0)
        delta_b = float(row_b.get("delta", 0) or 0)
        combined = round(abs(delta_a) + abs(delta_b), 1)
        volatile_matchup_rows.append(
            {
                "hero": hero_name,
                "team_a_delta": delta_a,
                "team_a_with_wr": float(row_a.get("with_wr", 0) or 0),
                "team_a_without_wr": float(row_a.get("without_wr", 0) or 0),
                "team_a_with_maps": float(row_a.get("with_maps", 0) or 0),
                "team_b_delta": delta_b,
                "team_b_with_wr": float(row_b.get("with_wr", 0) or 0),
                "team_b_without_wr": float(row_b.get("without_wr", 0) or 0),
                "team_b_with_maps": float(row_b.get("with_maps", 0) or 0),
                "combined_volatility": combined,
                "favored_side": team_a_name if delta_a > delta_b else team_b_name if delta_b > delta_a else "Even",
            }
        )
    volatile_matchup_rows.sort(
        key=lambda row: (row["combined_volatility"], row["hero"].lower()),
        reverse=True,
    )

    pivot_pressure_rows = []
    pivot_rows_a = team_a_model.get("pivot_rows", [])[:4]
    pivot_rows_b = team_b_model.get("pivot_rows", [])[:4]
    for enemy_pivot in pivot_rows_b:
        enemy_base = enemy_pivot.get("base_heroes", [])
        top_enemy_pivot = (enemy_pivot.get("pivots") or [None])[0]
        if not top_enemy_pivot:
            continue
        best_counter = None
        best_counter_score = None
        for our_pivot in pivot_rows_a:
            for option in our_pivot.get("pivots", []):
                shared = len(set(option.get("heroes", [])) & set(top_enemy_pivot.get("heroes", [])))
                counter_score = (shared * -1, option.get("diff_count", 0), option.get("rate", 0))
                if best_counter_score is None or counter_score < best_counter_score:
                    best_counter_score = counter_score
                    best_counter = {
                        "base_heroes": our_pivot.get("base_heroes", []),
                        "heroes": option.get("heroes", []),
                        "shared": shared,
                        "diff_count": option.get("diff_count", 0),
                    }
        pivot_pressure_rows.append(
            {
                "enemy_base": enemy_base,
                "enemy_pivot": top_enemy_pivot.get("heroes", []),
                "enemy_diff_count": top_enemy_pivot.get("diff_count", 0),
                "our_counter_base": best_counter.get("base_heroes", []) if best_counter else [],
                "our_counter_pivot": best_counter.get("heroes", []) if best_counter else [],
                "our_counter_diff_count": best_counter.get("diff_count", 0) if best_counter else 0,
                "shared_after_pivot": best_counter.get("shared", 0) if best_counter else 0,
            }
        )

    map_effect_rows = []
    for row in team_a_model.get("mode_hero_rows", [])[:5]:
        map_effect_rows.append(
            {
                "scope": "Hero",
                "team": team_a_name,
                "mode": row.get("mode", ""),
                "label": row.get("hero", ""),
                "delta": row.get("delta", 0),
            }
        )
    for row in team_b_model.get("mode_hero_rows", [])[:5]:
        map_effect_rows.append(
            {
                "scope": "Hero",
                "team": team_b_name,
                "mode": row.get("mode", ""),
                "label": row.get("hero", ""),
                "delta": row.get("delta", 0),
            }
        )
    for row in team_a_model.get("mode_comp_rows", [])[:3]:
        map_effect_rows.append(
            {
                "scope": "Comp",
                "team": team_a_name,
                "mode": row.get("mode", ""),
                "label": ", ".join(row.get("heroes", [])[:3]),
                "delta": row.get("delta", 0),
            }
        )
    for row in team_b_model.get("mode_comp_rows", [])[:3]:
        map_effect_rows.append(
            {
                "scope": "Comp",
                "team": team_b_name,
                "mode": row.get("mode", ""),
                "label": ", ".join(row.get("heroes", [])[:3]),
                "delta": row.get("delta", 0),
            }
        )
    map_effect_rows.sort(
        key=lambda row: (abs(float(row.get("delta", 0) or 0)), row.get("mode", "")),
        reverse=True,
    )

    equivalent_path_rows = []
    # Build a fast lookup: for each ban-line hero list, what heroes are in each team's ban lines.
    team_b_ban_hero_sets = [
        set(hero for hero in ban_line.get("heroes", []) if hero)
        for ban_line in team_b_model.get("ban_line_rows", [])[:8]
    ]
    team_a_ban_hero_sets = [
        set(hero for hero in ban_line.get("heroes", []) if hero)
        for ban_line in team_a_model.get("ban_line_rows", [])[:8]
    ]
    for row in team_a_model.get("equivalent_path_rows", [])[:4]:
        primary_ban_heroes = set(row.get("primary_bans", []))
        # Primary ban heroes that Team B commonly bans - these are the ones that would force Team A onto the alt path.
        enemy_pressure = []
        for ban_set in team_b_ban_hero_sets:
            for hero in ban_set & primary_ban_heroes:
                if hero not in enemy_pressure:
                    enemy_pressure.append(hero)
        equivalent_path_rows.append(
            {
                "team": team_a_name,
                "enemy_ban_pressure": enemy_pressure[:4],
                **row,
            }
        )
    for row in team_b_model.get("equivalent_path_rows", [])[:4]:
        primary_ban_heroes = set(row.get("primary_bans", []))
        # Primary ban heroes that Team A commonly bans - these are the ones that would force Team B onto the alt path.
        enemy_pressure = []
        for ban_set in team_a_ban_hero_sets:
            for hero in ban_set & primary_ban_heroes:
                if hero not in enemy_pressure:
                    enemy_pressure.append(hero)
        equivalent_path_rows.append(
            {
                "team": team_b_name,
                "enemy_ban_pressure": enemy_pressure[:4],
                **row,
            }
        )

    return {
        "status": "ready",
        "teams": [
            {"name": team_a_name, "model": team_a_model},
            {"name": team_b_name, "model": team_b_model},
        ],
        "contested_heroes": contested_heroes[:10],
        "force_matchup_rows": force_matchup_rows[:6],
        "deviation_rows": deviation_rows[:6],
        "volatile_matchup_rows": volatile_matchup_rows[:8],
        "pivot_pressure_rows": pivot_pressure_rows[:6],
        "map_effect_rows": map_effect_rows[:10],
        "equivalent_path_rows": equivalent_path_rows[:8],
        "ban_clash_rows": ban_clash_rows[:8],
        "comp_clash_rows": comp_clash_rows[:8],
        "map_consensus_rows": map_consensus_rows[:4],
        "ml_outlook_rows": ml_outlook_rows,
    }


