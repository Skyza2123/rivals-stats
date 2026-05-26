# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def build_draft_predictor(scrims: list[dict], raw_inputs: dict[str, str]) -> dict:
    cleaned_inputs = {
        field_key: (raw_inputs.get(field_key, "") or "").strip()
        for field_key in PREDICTOR_INPUT_ORDER
    }
    normalized_inputs = {
        field_key: _canonical_draft_hero(cleaned_inputs[field_key])
        for field_key in PREDICTOR_INPUT_ORDER
    }

    next_targets = []
    for group in PREDICTOR_GROUPS:
        missing = [item for item in group if not normalized_inputs[item[2]]]
        if missing:
            next_targets = missing
            break

    if not next_targets:
        return {
            "inputs": cleaned_inputs,
            "matching_maps": 0,
            "exact_matching_maps": 0,
            "training_maps": 0,
            "targets": [],
            "status": "complete",
        }

    exact_matching_maps = 0
    training_maps = 0
    exact_target_counts = {
        field_key: defaultdict(int)
        for _, _, field_key in next_targets
    }
    slot_prior_counts = {
        field_key: defaultdict(int)
        for _, _, field_key in next_targets
    }
    source_target_counts = {
        field_key: {
            source_field: defaultdict(lambda: defaultdict(int))
            for source_field in PREDICTOR_INPUT_ORDER
        }
        for _, _, field_key in next_targets
    }
    source_value_vocab = {
        field_key: {
            source_field: set()
            for source_field in PREDICTOR_INPUT_ORDER
        }
        for _, _, field_key in next_targets
    }
    exact_comp_counts = {
        "team1": defaultdict(int),
        "team2": defaultdict(int),
    }
    exact_match_rows = []
    show_exact_match_rows = any(
        field_key in {"t2_ban3", "t2_ban4", "t2_protect2"}
        for _, _, field_key in next_targets
    )
    comp_prior_counts = {
        "team1": defaultdict(int),
        "team2": defaultdict(int),
    }
    filled_inputs = {
        field_key: hero_name
        for field_key, hero_name in normalized_inputs.items()
        if hero_name
    }
    used_heroes = {hero_name for hero_name in normalized_inputs.values() if hero_name}

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            map_values = _predictor_map_values(map_entry)
            if not any(map_values.values()):
                continue
            training_maps += 1

            for _, _, field_key in next_targets:
                hero = map_values.get(field_key, "")
                if hero:
                    slot_prior_counts[field_key][hero] += 1
                    for source_field, source_hero in filled_inputs.items():
                        if map_values.get(source_field, "") == source_hero:
                            source_target_counts[field_key][source_field][source_hero][hero] += 1
                        observed_source_hero = map_values.get(source_field, "")
                        if observed_source_hero:
                            source_value_vocab[field_key][source_field].add(observed_source_hero)

            for team_slot in TEAM_SLOTS:
                richest_comp = _predictor_richest_comp(map_entry, team_slot)
                if richest_comp:
                    comp_prior_counts[team_slot][richest_comp] += 1

            if any(
                normalized_inputs[field_key] and map_values.get(field_key, "") != normalized_inputs[field_key]
                for field_key in PREDICTOR_INPUT_ORDER
            ):
                continue

            exact_matching_maps += 1
            for _, _, field_key in next_targets:
                hero = map_values.get(field_key, "")
                if hero:
                    exact_target_counts[field_key][hero] += 1

            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            if show_exact_match_rows:
                exact_match_rows.append(
                    {
                        "map_name": str(map_entry.get("map_name") or map_entry.get("map") or "").strip() or "Unknown Map",
                        "mode": str(map_entry.get("mode") or map_entry.get("map_mode") or "").strip() or "Unknown",
                        "result": get_map_outcome_for_slot(map_entry, our_team_slot) or "Unknown",
                        "team1_rows": [
                            {
                                "field_key": field_key,
                                "slot_label": _draft_slot_label(field_key.replace("t1_", "", 1)),
                                "hero": map_values.get(field_key, ""),
                            }
                            for field_key in PREDICTOR_INPUT_ORDER
                            if field_key.startswith("t1_") and map_values.get(field_key, "")
                        ],
                        "team2_rows": [
                            {
                                "field_key": field_key,
                                "slot_label": _draft_slot_label(field_key.replace("t2_", "", 1)),
                                "hero": map_values.get(field_key, ""),
                            }
                            for field_key in PREDICTOR_INPUT_ORDER
                            if field_key.startswith("t2_") and map_values.get(field_key, "")
                        ],
                    }
                )

            for team_slot in TEAM_SLOTS:
                richest_comp = _predictor_richest_comp(map_entry, team_slot)
                if richest_comp:
                    exact_comp_counts[team_slot][richest_comp] += 1

    target_rows = []
    for team_slot, slot_key, field_key in next_targets:
        exact_total = sum(exact_target_counts[field_key].values())
        support_by_hero: dict[str, int] = defaultdict(int)
        option_rows: list[dict] = []

        if exact_total:
            options = sorted(exact_target_counts[field_key].items(), key=lambda item: item[1], reverse=True)
            option_rows = [
                {
                    "hero": hero,
                    "count": count,
                    "rate": round((count / exact_total) * 100, 1) if exact_total else 0,
                }
                for hero, count in options
                if hero not in used_heroes
            ]
        else:
            slot_counts = slot_prior_counts[field_key]
            candidate_heroes = [hero for hero in slot_counts.keys() if hero not in used_heroes]
            slot_total = sum(slot_counts.values())
            alpha = 1.0
            scored_candidates: list[tuple[str, float]] = []

            if candidate_heroes and slot_total:
                vocab_size = max(len(candidate_heroes), 1)
                for hero in candidate_heroes:
                    hero_count = slot_counts.get(hero, 0)
                    support_by_hero[hero] = hero_count
                    # Empirical prior for this slot.
                    log_score = math.log((hero_count + alpha) / (slot_total + alpha * vocab_size))

                    # Naive Bayes context likelihood from observed draft history only.
                    for source_field, source_hero in filled_inputs.items():
                        source_counts_for_value = source_target_counts[field_key][source_field].get(source_hero, {})
                        joint_count = source_counts_for_value.get(hero, 0)
                        source_vocab_size = max(len(source_value_vocab[field_key][source_field]), 1)
                        log_score += math.log((joint_count + alpha) / (hero_count + alpha * (source_vocab_size + 1)))

                    scored_candidates.append((hero, log_score))

                if scored_candidates:
                    max_log_score = max(score for _, score in scored_candidates)
                    normalized = [
                        (hero, math.exp(score - max_log_score))
                        for hero, score in scored_candidates
                    ]
                    total_weight = sum(weight for _, weight in normalized)
                    option_rows = [
                        {
                            "hero": hero,
                            "count": support_by_hero.get(hero, 0),
                            "rate": round((weight / total_weight) * 100, 1) if total_weight else 0,
                        }
                        for hero, weight in sorted(normalized, key=lambda item: item[1], reverse=True)
                    ]

        target_rows.append(
            {
                "field_key": field_key,
                "team_label": "Team 1" if team_slot == "team1" else "Team 2",
                "slot_label": _draft_slot_label(slot_key),
                "options": option_rows[:8],
            }
        )

    likely_comps = []
    for team_slot in TEAM_SLOTS:
        comp_source = exact_comp_counts[team_slot] if exact_comp_counts[team_slot] else comp_prior_counts[team_slot]
        comp_total = sum(comp_source.values())
        comp_options = sorted(comp_source.items(), key=lambda item: item[1], reverse=True)
        if not comp_options:
            continue

        top_comp, top_count = comp_options[0]
        likely_comps.append(
            {
                "team_key": team_slot,
                "team_label": "Team 1" if team_slot == "team1" else "Team 2",
                "heroes": list(top_comp),
                "count": top_count,
                "rate": round((top_count / comp_total) * 100, 1) if comp_total else 0,
            }
        )

    return {
        "inputs": cleaned_inputs,
        "matching_maps": exact_matching_maps,
        "exact_matching_maps": exact_matching_maps,
        "exact_match_rows": exact_match_rows[:8] if show_exact_match_rows else [],
        "training_maps": training_maps,
        "targets": target_rows,
        "likely_comps": likely_comps,
        "status": "ready" if training_maps else "empty",
    }


