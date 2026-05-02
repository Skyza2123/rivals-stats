# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

@app.route("/draft-simulator")
def draft_simulator():
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    return render_template(
        "draft_simulator.html",
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        teams=teams,
    )


@app.route("/draft-reasoner")
def draft_reasoner():
    # Keep legacy URL working, but route everyone into the unified Machine UI.
    return redirect(url_for("machine", tab="reasoner"))


@app.route("/machine")
def machine():
    db = get_db()
    teams = db.execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    personal_team = db.execute(
        "SELECT id, name FROM teams WHERE is_personal = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    machine_history = SCRIMS + TOURNAMENT_MATCHES
    season_options = get_scrim_season_options(machine_history)
    default_season = get_current_season_from_recent_scrim(machine_history)
    has_unseasoned = any(not normalize_season_value(s.get("season", "")) for s in machine_history)
    return render_template(
        "machine.html",
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        teams=teams,
        maps=MAPS,
        season_options=season_options,
        default_season=default_season,
        has_unseasoned=has_unseasoned,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
        personal_team=personal_team,
    )


def _machine_chat_join(values: list[str], limit: int = 5) -> str:
    seen = set()
    cleaned = []
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return ", ".join(cleaned[:limit]) if cleaned else "none"


def _machine_chat_row_list(rows: list[dict], formatter, limit: int = 5) -> str:
    parts = []
    for row in (rows or [])[:limit]:
        try:
            text = formatter(row)
        except (TypeError, ValueError, KeyError):
            text = ""
        if text:
            parts.append(text)
    return "; ".join(parts) if parts else "none"


def _machine_agent_parse_slot_compare(message: str) -> dict | None:
    q = (message or "").strip().lower()
    if not q or not any(word in q for word in ("ban", "banning", "third", "fourth", "3rd", "4th", "difference", "compare")):
        return None

    hero_name = _machine_agent_parse_hero(message)
    if not hero_name:
        return None

    ordinal_slots = {
        "first": 1,
        "1st": 1,
        "one": 1,
        "second": 2,
        "2nd": 2,
        "two": 2,
        "third": 3,
        "3rd": 3,
        "three": 3,
        "fourth": 4,
        "4th": 4,
        "four": 4,
    }
    slots = []
    for word, number in ordinal_slots.items():
        if re.search(rf"\b{re.escape(word)}\b", q) and number not in slots:
            slots.append(number)
    for match in re.finditer(r"\bban\s*([1-4])\b", q):
        number = int(match.group(1))
        if number not in slots:
            slots.append(number)
    if len(slots) < 2:
        if 3 in slots:
            slots.append(4)
        elif 4 in slots:
            slots.insert(0, 3)
        else:
            return None
    return {"hero": hero_name, "slots": slots[:2]}


def _machine_agent_parse_hero(message: str) -> str:
    q = (message or "").strip().lower()
    for hero in sorted(HEROES, key=len, reverse=True):
        hero_key = hero.lower()
        if re.search(rf"(?<![a-z0-9]){re.escape(hero_key)}(?![a-z0-9])", q):
            return hero
    for alias, canonical in sorted(HERO_NAME_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        alias_text = alias.lower()
        if alias_text and re.search(rf"\b{re.escape(alias_text)}\b", q):
            return canonical
    return ""


def _machine_chat_filter_team_history(team_row, season_value: str, selected_map_name: str, include_scrims: bool, include_tournaments: bool) -> list[dict]:
    history = get_team_history_for_sources(
        team_row,
        include_scrims=include_scrims,
        include_tournaments=include_tournaments,
    )
    season_options = get_scrim_season_options(history)
    default_season = get_current_season_from_recent_scrim(history)
    has_unseasoned = any(not normalize_season_value(s.get("season", "")) for s in history)
    effective_default = "all" if not season_value or season_value.lower() == "all" else default_season
    selected_season = get_selected_season(
        season_value,
        season_options,
        allow_unspecified=has_unseasoned,
        default_season=effective_default,
        strict=True,
    )
    filtered = filter_scrims_by_season(history, selected_season)
    if selected_map_name and selected_map_name.lower() != "all":
        map_filtered = []
        for scrim in filtered:
            scrim_copy = dict(scrim)
            scrim_copy["maps"] = [
                m for m in scrim.get("maps", [])
                if ((m.get("map") or m.get("map_name") or "").strip().lower() == selected_map_name.lower())
            ]
            if scrim_copy["maps"]:
                map_filtered.append(scrim_copy)
        filtered = map_filtered
    return filtered


def _machine_agent_slot_timing_stats(history: list[dict], team_name: str, hero_name: str, slot_numbers: list[int]) -> dict:
    slot_keys = [f"ban{number}" for number in slot_numbers]
    rows = {
        slot_key: {
            "slot": slot_key,
            "label": f"Ban {slot_key[-1]}",
            "hero": hero_name,
            "maps": 0,
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "other_bans": Counter(),
            "protects": Counter(),
            "maps_by_name": Counter(),
        }
        for slot_key in slot_keys
    }
    total_complete_maps = 0

    for record in history:
        for map_entry in record.get("maps", []):
            if not isinstance(map_entry, dict):
                continue
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            draft = map_entry.get("draft", {})
            our_draft = draft.get(our_team_slot, {}) if isinstance(draft, dict) else {}
            if not isinstance(our_draft, dict):
                continue
            if not any(_canonical_draft_hero(our_draft.get(slot_key, "")) for slot_key in DRAFT_SLOT_ORDER):
                continue
            total_complete_maps += 1
            outcome = get_map_outcome_for_slot(map_entry, our_team_slot)
            map_name = (map_entry.get("map_name") or map_entry.get("map") or "").strip()
            for slot_key in slot_keys:
                if _canonical_draft_hero(our_draft.get(slot_key, "")) != hero_name:
                    continue
                row = rows[slot_key]
                row["maps"] += 1
                if outcome == "Win":
                    row["wins"] += 1
                elif outcome == "Loss":
                    row["losses"] += 1
                else:
                    row["ties"] += 1
                if map_name:
                    row["maps_by_name"][map_name] += 1
                for other_slot in ("ban1", "ban2", "ban3", "ban4"):
                    other_hero = _canonical_draft_hero(our_draft.get(other_slot, ""))
                    if other_slot != slot_key and other_hero:
                        row["other_bans"][other_hero] += 1
                for protect_slot in ("protect1", "protect2"):
                    protect_hero = _canonical_draft_hero(our_draft.get(protect_slot, ""))
                    if protect_hero:
                        row["protects"][protect_hero] += 1

    formatted = []
    for slot_key in slot_keys:
        row = rows[slot_key]
        maps = int(row["maps"])
        wins = int(row["wins"])
        losses = int(row["losses"])
        formatted.append(
            {
                "slot": slot_key,
                "label": row["label"],
                "hero": hero_name,
                "maps": maps,
                "wins": wins,
                "losses": losses,
                "ties": int(row["ties"]),
                "rate": round((maps / total_complete_maps) * 100, 1) if total_complete_maps else 0.0,
                "win_rate": round((wins / (wins + losses)) * 100, 1) if (wins + losses) else 0.0,
                "common_other_bans": [hero for hero, _count in row["other_bans"].most_common(4)],
                "common_protects": [hero for hero, _count in row["protects"].most_common(3)],
                "common_maps": [map_name for map_name, _count in row["maps_by_name"].most_common(3)],
            }
        )

    leader = None
    if len(formatted) >= 2:
        left, right = formatted[0], formatted[1]
        if left["maps"] and right["maps"]:
            wr_delta = round(left["win_rate"] - right["win_rate"], 1)
            if abs(wr_delta) >= 0.1:
                leader = left["label"] if wr_delta > 0 else right["label"]
        elif left["maps"] or right["maps"]:
            leader = left["label"] if left["maps"] > right["maps"] else right["label"]

    return {
        "team": team_name,
        "hero": hero_name,
        "total_draft_maps": total_complete_maps,
        "slots": formatted,
        "leader": leader,
    }


def _machine_agent_opposite_slot(team_slot: str) -> str:
    return "team2" if team_slot == "team1" else "team1"


def _machine_agent_player_names_for_team(team_id: int | None) -> list[str]:
    if not team_id:
        return []
    rows = get_db().execute(
        """
        SELECT name FROM players WHERE team_id = ?
        UNION
        SELECT name FROM enemy_players WHERE enemy_team_id = ?
        ORDER BY name COLLATE NOCASE
        """,
        (team_id, team_id),
    ).fetchall()
    return [(row["name"] or "").strip() for row in rows if (row["name"] or "").strip()]


def _machine_agent_find_player_name(message: str, team_ids: list[int | None]) -> str:
    text = (message or "").lower()
    candidates = []
    for team_id in team_ids:
        for name in _machine_agent_player_names_for_team(team_id):
            key = name.lower()
            if key and key in text:
                candidates.append((len(key), name))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]

    history_names = set()
    for team_id in team_ids:
        if not team_id:
            continue
        row = get_db().execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
        if row is None:
            continue
        for record in get_team_history_for_sources(row, include_scrims=True, include_tournaments=True):
            for map_entry in record.get("maps", []):
                for section in map_entry.get("comp", []):
                    if not isinstance(section, dict):
                        continue
                    for side in TEAM_SLOTS:
                        for slot in section.get(side, []):
                            if isinstance(slot, dict):
                                player = (slot.get("player") or "").strip()
                                if player:
                                    history_names.add(player)
    for name in sorted(history_names, key=len, reverse=True):
        if name.lower() in text:
            return name
    return ""


def _machine_agent_parse_player_pivot(message: str, context: dict) -> dict | None:
    q = (message or "").lower()
    if not any(word in q for word in ("pivot", "swap", "switch", "go to", "play if", "likely to")):
        return None
    if not any(word in q for word in ("ban", "banned", "deny", "remove", "if i ban", "if we ban")):
        return None
    hero = _machine_agent_parse_hero(message)
    if not hero:
        return None
    team_ids = [context.get("team_b_id"), context.get("team_a_id")]
    player = _machine_agent_find_player_name(message, team_ids)
    if not player:
        return None
    target_team_id = None
    for team_id in team_ids:
        if player in _machine_agent_player_names_for_team(team_id):
            target_team_id = team_id
            break
    return {
        "player": player,
        "banned_hero": hero,
        "target_team_id": target_team_id or context.get("team_b_id") or context.get("team_a_id"),
    }


def _machine_agent_player_pivot_missing_response(message: str, context: dict) -> str:
    hero = _machine_agent_parse_hero(message)
    player = _machine_agent_find_player_name(message, [context.get("team_b_id"), context.get("team_a_id")])
    if not player and not hero:
        return "Which player and banned hero should I check? Example: `Who does Polly pivot to if we ban Captain America?`"
    if not player:
        return f"Which player should I check for the {hero} ban?"
    return f"Which banned hero should I use for {player}'s pivot read?"


def _machine_agent_player_pivot_stats(
    history: list[dict],
    player_name: str,
    banned_hero: str,
) -> dict:
    player_key = (player_name or "").strip().lower()
    banned_key = (banned_hero or "").strip().lower()
    banned_hero = _canonical_draft_hero(banned_hero)
    pivot_counts: Counter[str] = Counter()
    pivot_wins: Counter[str] = Counter()
    normal_counts: Counter[str] = Counter()
    normal_wins: Counter[str] = Counter()
    banned_maps = 0
    player_banned_maps = 0
    total_player_maps = 0

    for record in history:
        for map_entry in record.get("maps", []):
            if not isinstance(map_entry, dict):
                continue
            team_slot = map_entry.get("our_team_slot", "team1")
            if team_slot not in TEAM_SLOTS:
                team_slot = "team1"
            opponent_slot = _machine_agent_opposite_slot(team_slot)
            draft = map_entry.get("draft", {})
            opponent_draft = draft.get(opponent_slot, {}) if isinstance(draft, dict) else {}
            if not isinstance(opponent_draft, dict):
                opponent_draft = {}
            opponent_bans = {
                _canonical_draft_hero(opponent_draft.get(slot_key, ""))
                for slot_key in ("ban1", "ban2", "ban3", "ban4")
            }
            hero_was_banned = banned_hero in opponent_bans
            if hero_was_banned:
                banned_maps += 1

            heroes_this_map = set()
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(team_slot, []):
                    if not isinstance(slot, dict):
                        continue
                    if (slot.get("player", "") or "").strip().lower() != player_key:
                        continue
                    hero = _canonical_draft_hero(slot.get("hero", ""))
                    if hero:
                        heroes_this_map.add(hero)

            if not heroes_this_map:
                continue

            total_player_maps += 1
            result = get_map_outcome_for_slot(map_entry, team_slot)
            for hero in heroes_this_map:
                normal_counts[hero] += 1
                if result == "Win":
                    normal_wins[hero] += 1
            if hero_was_banned:
                player_banned_maps += 1
                for hero in heroes_this_map:
                    pivot_counts[hero] += 1
                    if result == "Win":
                        pivot_wins[hero] += 1

    def rows_from_counts(counts: Counter[str], wins: Counter[str], total: int, limit: int = 5) -> list[dict]:
        rows = []
        for hero, count in counts.most_common(limit):
            rows.append({
                "hero": hero,
                "count": int(count),
                "rate": round((count / total) * 100, 1) if total else 0.0,
                "win_rate": round((wins[hero] / count) * 100, 1) if count else 0.0,
            })
        return rows

    return {
        "player": player_name,
        "banned_hero": banned_hero,
        "banned_maps": banned_maps,
        "player_banned_maps": player_banned_maps,
        "total_player_maps": total_player_maps,
        "pivots": rows_from_counts(pivot_counts, pivot_wins, player_banned_maps),
        "fallback": rows_from_counts(normal_counts, normal_wins, total_player_maps),
    }


def _machine_agent_player_pivot_answer(stats: dict, team_name: str) -> str:
    pivots = stats.get("pivots", [])
    fallback = stats.get("fallback", [])
    player = stats.get("player", "That player")
    banned_hero = stats.get("banned_hero", "that hero")
    if pivots:
        top = pivots[0]
        return (
            f"If you ban {banned_hero}, I expect {player} to land on {top['hero']}.\n\n"
            f"Sample: {top['count']} of {stats.get('player_banned_maps', 0)} matching maps | {top['rate']}% pivot rate | {top['win_rate']}% WR."
        )
    if fallback:
        top = fallback[0]
        return (
            f"I do not have a clean ban-triggered sample for {player} yet.\n\n"
            f"Fallback: {top['hero']} | {top['count']} of {stats.get('total_player_maps', 0)} player maps | {top['rate']}% pick rate | {top['win_rate']}% WR."
        )
    return (
        f"I do not have a usable pivot sample for {player} in these filters.\n\n"
        "Try a wider season or all maps."
    )


def _machine_chat_build_context(team_a_id: int | None, team_b_id: int | None, season_value: str, selected_map_name: str, include_scrims: bool, include_tournaments: bool) -> tuple[str, dict]:
    db = get_db()
    source_label = []
    if include_scrims:
        source_label.append("scrims")
    if include_tournaments:
        source_label.append("tournaments")

    if not team_a_id or not team_b_id:
        teams = db.execute("SELECT name FROM teams ORDER BY name COLLATE NOCASE LIMIT 18").fetchall()
        return (
            "No matchup selected. Available teams include: "
            + _machine_chat_join([row["name"] for row in teams], 18)
            + ". Ask the user to pick both teams for matchup-specific draft advice.",
            {"has_matchup": False},
        )

    team_rows = db.execute(
        "SELECT * FROM teams WHERE id IN (?, ?)", (team_a_id, team_b_id)
    ).fetchall()
    team_lookup = {row["id"]: row for row in team_rows}
    team_a = team_lookup.get(team_a_id)
    team_b = team_lookup.get(team_b_id)
    if team_a is None or team_b is None:
        abort(404)

    a_history = _machine_chat_filter_team_history(team_a, season_value, selected_map_name, include_scrims, include_tournaments)
    b_history = _machine_chat_filter_team_history(team_b, season_value, selected_map_name, include_scrims, include_tournaments)
    model = build_matchup_tree_model(team_a["name"], a_history, team_b["name"], b_history)
    team_models = model.get("teams", [])
    a_model = (team_models[0].get("model", {}) if len(team_models) > 0 else {})
    b_model = (team_models[1].get("model", {}) if len(team_models) > 1 else {})

    def comfort_rows(team_model):
        return _machine_chat_row_list(
            team_model.get("comfort_core_rows", []),
            lambda r: f"{r.get('hero')} ({r.get('comfort_rate', 0)}% comfort, {r.get('adjusted_win_rate', r.get('win_rate', 0))}% adj WR)",
            8,
        )

    def line_rows(team_model, key):
        return _machine_chat_row_list(
            team_model.get(key, []),
            lambda r: f"{_machine_chat_join(r.get('heroes', []), 6)} ({r.get('rate', 0)}% rate, {r.get('line_win_rate', r.get('comp_win_rate', 0))}% WR)",
            4,
        )

    recommended_bans = []
    recommended_protects = []
    target_comp = []

    def add_unique(target: list[str], values: list[str], limit: int) -> None:
        existing = {v.lower() for v in target}
        for value in values:
            hero = (value or "").strip()
            key = hero.lower()
            if hero and key not in existing:
                target.append(hero)
                existing.add(key)
            if len(target) >= limit:
                break

    for row in model.get("force_matchup_rows", [])[:3]:
        add_unique(recommended_bans, row.get("our_bans", []), 6)
        add_unique(recommended_protects, row.get("our_protects", []), 3)
        if not target_comp:
            add_unique(target_comp, row.get("our_comp", []), 6)
    add_unique(recommended_bans, [r.get("hero", "") for r in model.get("volatile_matchup_rows", [])], 6)
    add_unique(recommended_bans, [r.get("hero", "") for r in b_model.get("comfort_core_rows", [])], 6)
    if not target_comp:
        comp_row = (a_model.get("comp_rows") or [{}])[0]
        add_unique(target_comp, comp_row.get("heroes", []), 6)

    enemy_comps = [
        {
            "heroes": row.get("heroes", [])[:6],
            "rate": row.get("rate", 0),
            "win_rate": row.get("comp_win_rate", row.get("win_rate", 0)),
            "confidence": row.get("ml_confidence", 0),
        }
        for row in b_model.get("comp_rows", [])[:4]
    ]
    pivot_predictions = [
        {
            "base": row.get("enemy_base", row.get("base_heroes", []))[:6],
            "pivot": row.get("enemy_pivot", [])[:6],
            "counter": row.get("our_counter_pivot", [])[:6],
            "diff_count": row.get("enemy_diff_count", 0),
            "counter_diff_count": row.get("our_counter_diff_count", 0),
        }
        for row in model.get("pivot_pressure_rows", [])[:4]
    ]
    comp_confidence = 0
    comp_wr = 0
    for row in a_model.get("comp_rows", []):
        if set(target_comp) == set(row.get("heroes", [])):
            comp_confidence = row.get("ml_confidence", 0)
            comp_wr = row.get("ml_win_prob", row.get("comp_win_rate", 0))
            break

    context_lines = [
        f"Matchup: {team_a['name']} vs {team_b['name']}.",
        f"Filters: season={season_value or 'all'}, map={selected_map_name or 'all'}, sources={_machine_chat_join(source_label) or 'none'}.",
        f"Data volume: {team_a['name']} {len(a_history)} records, {team_b['name']} {len(b_history)} records.",
        f"Recommended ban targets: {_machine_chat_join(recommended_bans, 6)}.",
        f"Recommended protects: {_machine_chat_join(recommended_protects, 3)}.",
        f"Target comp path: {_machine_chat_join(target_comp, 6)}.",
        f"{team_a['name']} comfort heroes: {comfort_rows(a_model)}.",
        f"{team_b['name']} comfort heroes: {comfort_rows(b_model)}.",
        f"{team_a['name']} common ban lines: {line_rows(a_model, 'ban_line_rows')}.",
        f"{team_b['name']} common ban lines: {line_rows(b_model, 'ban_line_rows')}.",
        f"{team_a['name']} common comps: {line_rows(a_model, 'comp_rows')}.",
        f"{team_b['name']} common comps: {line_rows(b_model, 'comp_rows')}.",
        "Contested heroes: "
        + _machine_chat_row_list(
            model.get("contested_heroes", []),
            lambda r: f"{r.get('hero')} ({team_a['name']} {r.get('team_a_comfort', 0)}%, {team_b['name']} {r.get('team_b_comfort', 0)}%)",
            8,
        )
        + ".",
        "Force paths: "
        + _machine_chat_row_list(
            model.get("force_matchup_rows", []),
            lambda r: f"{team_a['name']} comp {_machine_chat_join(r.get('our_comp', []))}; bans {_machine_chat_join(r.get('our_bans', []))}; expected enemy {_machine_chat_join(r.get('enemy_comp', []))}",
            4,
        )
        + ".",
        "Volatile heroes: "
        + _machine_chat_row_list(
            model.get("volatile_matchup_rows", []),
            lambda r: f"{r.get('hero')} favors {r.get('favored_side')} (delta {r.get('delta', 0)})",
            6,
        )
        + ".",
        "Map consensus: "
        + _machine_chat_row_list(
            model.get("map_consensus_rows", []),
            lambda r: f"{r.get('label')}: {_machine_chat_join([o.get('map_name') for o in r.get('options', [])], 4)}",
            4,
        )
        + ".",
    ]
    return "\n".join(context_lines), {
        "has_matchup": True,
        "team_a": team_a["name"],
        "team_b": team_b["name"],
        "model_status": model.get("status", "ready"),
        "visuals": {
            "recommended_bans": recommended_bans[:6],
            "recommended_protects": recommended_protects[:3],
            "target_comp": target_comp[:6],
            "our_comfort": [
                row.get("hero", "")
                for row in a_model.get("comfort_core_rows", [])[:6]
                if (row.get("hero", "") or "").strip()
            ],
            "enemy_comfort": [
                row.get("hero", "")
                for row in b_model.get("comfort_core_rows", [])[:6]
                if (row.get("hero", "") or "").strip()
            ],
            "contested": [
                row.get("hero", "")
                for row in model.get("contested_heroes", [])[:6]
                if (row.get("hero", "") or "").strip()
            ],
            "volatile": [
                row.get("hero", "")
                for row in model.get("volatile_matchup_rows", [])[:6]
                if (row.get("hero", "") or "").strip()
            ],
            "volatile_rows": [
                {
                    "hero": row.get("hero", ""),
                    "favored_side": row.get("favored_side", ""),
                    "delta": row.get("delta", 0),
                }
                for row in model.get("volatile_matchup_rows", [])[:8]
                if (row.get("hero", "") or "").strip()
            ],
            "likely_next_pick": target_comp[:1] or [
                row.get("hero", "")
                for row in a_model.get("comfort_core_rows", [])[:1]
                if (row.get("hero", "") or "").strip()
            ],
            "enemy_comps": enemy_comps,
            "pivot_predictions": pivot_predictions,
            "our_comp_rows": [
                {
                    "heroes": row.get("heroes", [])[:6],
                    "rate": row.get("rate", 0),
                    "win_rate": row.get("comp_win_rate", row.get("line_win_rate", row.get("win_rate", 0))),
                }
                for row in a_model.get("comp_rows", [])[:5]
                if row.get("heroes")
            ],
            "confidence": {
                "target_comp": target_comp[:6],
                "confidence": round(float(comp_confidence or 0), 1),
                "win_rate": round(float(comp_wr or 0), 1),
                "sample": len(a_history),
            },
            "map_consensus": [
                {
                    "label": row.get("label", ""),
                    "maps": [
                        opt.get("map_name", "")
                        for opt in row.get("options", [])[:4]
                        if (opt.get("map_name", "") or "").strip()
                    ],
                }
                for row in model.get("map_consensus_rows", [])[:4]
            ],
        },
    }


def _machine_chat_local_answer(message: str, context_text: str, meta: dict) -> str:
    if not meta.get("has_matchup"):
        return "Pick both teams in the agent controls and I can produce a matchup-specific draft read."

    question = (message or "").lower()
    lines = context_text.splitlines()
    wanted = []
    if any(word in question for word in ("ban", "bans", "protect", "counter")):
        wanted = [
            line for line in lines
            if line.startswith("Recommended ban targets")
            or line.startswith("Recommended protects")
            or line.startswith("Target comp path")
            or "common ban lines" in line
            or line.startswith("Volatile heroes")
            or line.startswith("Contested heroes")
        ]
    elif any(word in question for word in ("map", "maps")):
        wanted = [line for line in lines if line.startswith("Map consensus") or line.startswith("Filters") or "Data volume" in line]
    elif any(word in question for word in ("comp", "composition", "play")):
        wanted = [line for line in lines if line.startswith("Target comp path") or line.startswith("Recommended protects") or "common comps" in line or "Force paths:" in line or "comfort heroes" in line]
    else:
        wanted = [line for line in lines if line.startswith("Matchup") or "comfort heroes" in line or line.startswith("Contested") or line.startswith("Force paths")]

    return "Draft agent read\n\n" + "\n".join(wanted[:6])


def _machine_agent_is_info_request(message: str, intent: str) -> bool:
    q = (message or "").lower()
    # Snapshot/profile style asks should always route through site-data lookup,
    # even if the intent parser classifies them as draft-oriented (e.g., comp snapshot).
    snapshot_phrases = (
        "snapshot",
        "profile",
        "overview",
        "breakdown",
        "summary",
        "tell me about",
        "show me",
        "give me",
        "what do you know about",
        "info on",
        "information on",
        "information about",
    )
    if any(phrase in q for phrase in snapshot_phrases):
        return True
    if "tournament" in q and any(token in q for token in ("comp", "composition", "player", "team", "hero", "map", "stats", "record", "history")):
        return True

    if intent in {
        "ban",
        "protect",
        "comp",
        "risk",
        "check",
        "next_pick",
        "enemy_comps",
        "ban_impact",
        "player_pivot",
        "pivot",
        "confidence",
        "contested",
        "comfort",
        "hero_volatility",
        "slot_compare",
    }:
        return False
    return any(
        phrase in q
        for phrase in (
            "who is",
            "what is",
            "what are",
            "what was",
            "show",
            "tell me",
            "give me",
            "list",
            "history",
            "record",
            "stats",
            "stat",
            "overview",
            "profile",
            "snapshot",
            "breakdown",
            "hero pool",
            "heroes does",
            "plays",
            "played",
            "how many",
            "recent scrim",
            "recent match",
            "our team",
            "my team",
            "our roster",
            "our players",
            "our comps",
            "our heroes",
            "our maps",
            "everything about",
            "all about",
            "info on",
            "summary",
        )
    )


def _machine_agent_site_answer(message: str, season_value: str | None = None) -> dict | None:
    from draft_engine.site_context import search_site

    season = None if not season_value or season_value == "all" else season_value
    result = search_site(get_db(), message, season)
    sources = result.get("sources_used", [])
    if not sources or sources == ["none — no matching entity found in site data"]:
        return None

    def hero_bits(rows: list[dict], limit: int = 6) -> str:
        values = []
        for row in (rows or [])[:limit]:
            hero = (row.get("hero") or "").strip()
            if not hero:
                continue
            appearances = row.get("appearances") or row.get("played_count") or row.get("count") or row.get("co_appearances") or 0
            has_record = any(key in row for key in ("wins", "losses", "played_wins", "played_losses"))
            wins = row.get("wins", row.get("played_wins", 0)) or 0
            losses = row.get("losses", row.get("played_losses", 0)) or 0
            if appearances and has_record and (wins + losses):
                wr = round((wins / (wins + losses)) * 100, 1)
                values.append(f"{hero} ({appearances}, {wr}% WR)")
            elif appearances:
                values.append(f"{hero} ({appearances})")
            else:
                values.append(hero)
        return _machine_chat_join(values, limit)

    q = (message or "").lower()
    wants_scrims = any(phrase in q for phrase in ("scrim", "scrims", "history", "recent match", "recent matches", "recent game", "results"))
    wants_comps = any(phrase in q for phrase in (
        "what comp", "what comps", "comps do", "comp do", "comps does", "comp does",
        "comps can", "comp can", "comps will", "comp will", "comps they", "comp they",
        "comp style", "comp options", "play in", "run in", "draft style",
    ))
    wants_player = any(phrase in q for phrase in ("who is", "who plays", "heroes does", "hero pool", "player"))
    wants_hero = any(phrase in q for phrase in (
        "tell me about", "banned", "protected", "how much does", "how often does",
        "how many times", "ban rate", "does ban",
    )) and not any(phrase in q for phrase in ("team profile", "team overview", "profile", "overview"))
    wants_team = wants_comps or any(phrase in q for phrase in ("overview", "profile", "snapshot", "breakdown", "everything about", "all about", "tell me about"))
    wants_map = any(phrase in q for phrase in ("map", "maps")) and any(
        phrase in q
        for phrase in (
            "record", "stats", "played", "good on", "best on", "best map", "strongest map", "favorite map", "go to map",
        )
    )
    wants_bans = any(phrase in q for phrase in ("ban", "bans", "banned", "ban rate", "ban stats"))

    section_map = {}

    # --- Player section ---
    players = result.get("players", []) or []
    if players:
        player = players[0]
        section_map["player"] = (
            f"{player.get('player_name') or 'That player'} is on {player.get('team_name') or 'the roster'}"
            f"{(' as ' + player.get('role')) if player.get('role') else ''}. Main heroes: {hero_bits(player.get('heroes', []), 6)}."
        )

    # --- Hero section ---
    heroes = result.get("heroes", []) or []
    if heroes:
        hero = heroes[0]
        partners = _machine_chat_join([row.get("partner", "") for row in hero.get("pair_partners", [])], 5)
        section_map["hero"] = (
            f"{hero.get('hero') or 'That hero'} for {hero.get('team') or result.get('personal_team') or 'our team'}: "
            f"{hero.get('played_count', 0)} plays, {hero.get('played_wins', 0)}-{hero.get('played_losses', 0)}, "
            f"banned {hero.get('ban_count', 0)}x, protected {hero.get('protect_count', 0)}x. "
            f"Best partners: {partners}."
        )

    # --- Teams section ---
    teams = result.get("teams", []) or []
    team_profile_visuals: dict = {}
    if teams:
        team = teams[0]
        bias = hero_bits(team.get("hero_bias", []), 8)
        pair_cores = []
        for row in (team.get("pair_cores", []) or [])[:5]:
            left = (row.get("hero_a") or "").strip()
            right = (row.get("hero_b") or "").strip()
            co = row.get("co_appearances", 0)
            if left and right:
                pair_cores.append(f"{left}/{right} ({co}x)")
        map_stats = []
        for row in (team.get("map_stats", []) or [])[:5]:
            map_name = (row.get("map_name") or "").strip()
            if map_name:
                w = row.get("wins", 0)
                l = row.get("losses", 0)
                played = row.get("played", 0)
                map_stats.append(f"{map_name} {w}-{l}/{played}")
        # Player pools
        player_lines = []
        for pp in (team.get("player_pools") or [])[:6]:
            pname = pp.get("player_name", "")
            role = pp.get("role", "")
            top_h = hero_bits(pp.get("heroes", []), 4)
            if pname:
                player_lines.append(f"{pname}{(' (' + role + ')') if role else ''}: {top_h}")
        team_has_data = bool(bias) or bool(pair_cores) or bool(map_stats)
        if team_has_data:
            team_text = (
                f"{team.get('team') or 'That team'} profile:\n"
                f"  Hero bias: {bias}.\n"
                f"  Core pairs: {_machine_chat_join(pair_cores, 5)}.\n"
                f"  Map record: {_machine_chat_join(map_stats, 5)}."
            )
        else:
            team_text = f"No draft-engine data loaded yet for {team.get('team') or 'that team'} — comp data will appear after the ETL runs."
        if wants_bans:
            ban_rows = sorted(
                [row for row in (team.get("hero_bias") or []) if (row.get("ban_count") or 0) > 0],
                key=lambda row: row.get("ban_count", 0),
                reverse=True,
            )
            ban_text = _machine_chat_join(
                [f"{row.get('hero', '')} ({row.get('ban_count', 0)}x)" for row in ban_rows if row.get("hero")],
                6,
            )
            if ban_text:
                team_text += f"\n  Most banned: {ban_text}."
        if player_lines:
            team_text += "\n  Player pools:\n    " + "\n    ".join(player_lines)
        section_map["team"] = team_text

        # Build structured team profile for card rendering
        team_profile_visuals = {
            "team_name": team.get("team") or "",
            "hero_bias": [
                {
                    "hero": b.get("hero", ""),
                    "played": b.get("played_count", 0),
                    "wins": b.get("played_wins", 0),
                    "losses": b.get("played_losses", 0),
                    "banned": b.get("ban_count", 0),
                }
                for b in (team.get("hero_bias") or [])[:10]
                if b.get("hero")
            ],
            "pair_cores": [
                {"hero_a": r.get("hero_a", ""), "hero_b": r.get("hero_b", ""), "co_appearances": r.get("co_appearances", 0)}
                for r in (team.get("pair_cores") or [])[:5]
                if r.get("hero_a") and r.get("hero_b")
            ],
            "map_stats": [
                {"map_name": r.get("map_name", ""), "wins": r.get("wins", 0), "losses": r.get("losses", 0), "played": r.get("played", 0)}
                for r in (team.get("map_stats") or [])[:6]
                if r.get("map_name")
            ],
            "player_pools": [
                {
                    "player_name": pp.get("player_name", ""),
                    "role": pp.get("role", ""),
                    "heroes": [
                        {"hero": h.get("hero", ""), "appearances": h.get("appearances", 0), "wins": h.get("wins", 0), "losses": h.get("losses", 0)}
                        for h in (pp.get("heroes") or [])[:5]
                        if h.get("hero")
                    ],
                }
                for pp in (team.get("player_pools") or [])[:8]
                if pp.get("player_name")
            ],
        }

    # --- Map section ---
    maps = result.get("maps", []) or []
    if maps:
        map_row = maps[0]
        section_map["map"] = (
            f"On {map_row.get('map_name') or 'that map'}, {map_row.get('team') or result.get('personal_team') or 'our team'} is "
            f"{map_row.get('wins', 0)}-{map_row.get('losses', 0)} over {map_row.get('played', 0)} maps. "
            f"Most played: {hero_bits(map_row.get('hero_freq', []), 5)}. "
            f"Most common bans: {hero_bits(map_row.get('bans', []), 4)}."
        )

    # --- Scrim history section ---
    scrims = result.get("scrims_summary", []) or []
    scrim_rows_for_card: list = []
    if scrims:
        snippets = []
        for row in scrims[:5]:
            enemy = row.get("enemy_team") or row.get("opponent") or "unknown"
            scrim_date = row.get("scrim_date") or "undated"
            maps_played = row.get("maps", []) or []
            wins = sum(1 for m in maps_played if (m.get("result") or "").lower() == "win")
            losses = sum(1 for m in maps_played if (m.get("result") or "").lower() == "loss")
            result_str = f" ({wins}-{losses})" if wins + losses else ""
            snippets.append(f"{scrim_date} vs {enemy}{result_str}")
            scrim_rows_for_card.append({"date": scrim_date, "opponent": enemy, "wins": wins, "losses": losses})
        section_map["scrims"] = f"Recent scrim history: {'; '.join(snippets)}."

    # Build preferred ordering based on question type
    sections: list[str] = []
    seen: set[str] = set()
    preferred = []
    if wants_player:
        preferred.append("player")
    if wants_hero or (section_map.get("hero") and wants_bans):
        preferred.append("hero")
    if wants_map:
        preferred.append("map")
    if wants_scrims:
        preferred.append("scrims")
    if wants_team:
        preferred.extend(["team", "player"])
        if wants_scrims:
            preferred.append("scrims")
    # always include anything available in fallback order
    preferred.extend(["player", "hero", "team", "map"])
    if not wants_comps:
        preferred.append("scrims")
    for key in preferred:
        if key in seen or not section_map.get(key):
            continue
        if wants_scrims and key == "team" and not wants_team:
            continue
        sections.append(section_map[key])
        seen.add(key)
        if len(sections) >= 5:
            break

    if not sections:
        return None

    # Only attach the team profile card visuals when we have team data
    profile_visuals: dict = {}
    if teams and team_profile_visuals:
        profile_visuals["team_profile"] = team_profile_visuals
        if scrim_rows_for_card:
            profile_visuals["team_profile"]["recent_scrims"] = scrim_rows_for_card

    # Helper: load ban impact rows for a player name
    def _load_ban_impact(player_name: str) -> list[dict]:
        try:
            from draft_engine.site_context import get_scrim_history
            scrims = get_scrim_history(get_db(), result.get("personal_team") or "", vs=None, season=season, limit=100)
            rows = build_player_ban_impact(player_name, scrims)
            return rows
        except Exception:
            return []

    # Player profile card visual
    if players:
        p = players[0]
        ban_rows = _load_ban_impact(p.get("player_name", ""))
        profile_visuals["player_profile"] = {
            "player_name": p.get("player_name", ""),
            "team_name": p.get("team_name", ""),
            "role": p.get("role", ""),
            "heroes": [
                {
                    "hero": h.get("hero", ""),
                    "appearances": h.get("appearances", 0),
                    "wins": h.get("wins", 0),
                    "losses": h.get("losses", 0),
                }
                for h in (p.get("heroes") or [])[:10]
                if h.get("hero")
            ],
            "ban_impact": [
                {
                    "hero": r["hero"],
                    "hero_maps": r["hero_maps"],
                    "hero_wr": r["hero_wr"],
                    "times_banned": r["times_banned"],
                    "wr_when_banned": r["wr_when_banned"],
                    "wr_delta": r["wr_delta"],
                    "pivots": [
                        {"hero": pv["hero"], "count": pv["count"], "wr": pv.get("wr")}
                        for pv in (r.get("all_pivots") or [])[:4]
                    ],
                }
                for r in ban_rows[:6]
                if r.get("hero")
            ],
        }

    # Hero profile card visual
    if heroes:
        h = heroes[0]
        hero_name_key = (h.get("hero") or "").strip().lower()
        # Find which players on the personal team play this hero, gather their ban impact row for it
        hero_player_impacts: list[dict] = []
        try:
            from draft_engine.site_context import get_scrim_history
            scrims_for_hero = get_scrim_history(get_db(), result.get("personal_team") or "", vs=None, season=season, limit=100)
            # Get all players on personal team
            rows_players = get_db().execute(
                """SELECT p.name FROM players p
                   JOIN teams t ON p.team_id = t.id
                   WHERE t.is_personal = 1"""
            ).fetchall()
            for pr in rows_players:
                pname = pr[0]
                bi_rows = build_player_ban_impact(pname, scrims_for_hero)
                for bi in bi_rows:
                    if (bi.get("hero") or "").strip().lower() == hero_name_key and bi.get("hero_maps", 0) >= 3:
                        hero_player_impacts.append({
                            "player_name": pname,
                            "hero_maps": bi["hero_maps"],
                            "hero_wr": bi["hero_wr"],
                            "times_banned": bi["times_banned"],
                            "wr_when_banned": bi["wr_when_banned"],
                            "wr_delta": bi["wr_delta"],
                            "pivots": [
                                {"hero": pv["hero"], "count": pv["count"], "wr": pv.get("wr")}
                                for pv in (bi.get("all_pivots") or [])[:4]
                            ],
                        })
        except Exception:
            pass

        profile_visuals["hero_profile"] = {
            "hero": h.get("hero", ""),
            "team": h.get("team", ""),
            "played_count": h.get("played_count", 0),
            "played_wins": h.get("played_wins", 0),
            "played_losses": h.get("played_losses", 0),
            "ban_count": h.get("ban_count", 0),
            "protect_count": h.get("protect_count", 0),
            "pair_partners": [
                {"partner": pp.get("partner", ""), "co_appearances": pp.get("co_appearances", 0), "wins": pp.get("wins", 0)}
                for pp in (h.get("pair_partners") or [])[:8]
                if pp.get("partner")
            ],
            "player_impacts": hero_player_impacts,
        }

    profile_links: list[dict] = []
    if teams:
        team_name = (teams[0].get("team") or "").strip()
        if team_name:
            team_row = get_db().execute("SELECT id, name FROM teams WHERE lower(name)=lower(?) LIMIT 1", (team_name,)).fetchone()
            if team_row:
                profile_links.append({
                    "type": "team",
                    "label": f"Open {team_row['name']} team profile",
                    "url": url_for("team_detail", team_id=team_row["id"]),
                })
    if players:
        player_name = (players[0].get("player_name") or "").strip()
        player_team = (players[0].get("team_name") or "").strip()
        if player_name and player_team:
            player_row = get_db().execute(
                """SELECT p.id AS player_id, t.id AS team_id, p.name AS player_name
                   FROM players p JOIN teams t ON p.team_id = t.id
                   WHERE lower(t.name)=lower(?) AND lower(p.name)=lower(?)
                   LIMIT 1""",
                (player_team, player_name),
            ).fetchone()
            if player_row:
                profile_links.append({
                    "type": "player",
                    "label": f"Open {player_row['player_name']} player profile",
                    "url": url_for("player_detail", team_id=player_row["team_id"], player_id=player_row["player_id"]),
                })

    return {
        "answer": "\n\n".join(sections),
        "meta": {
            "has_matchup": False,
            "intent": "site_info",
            "needs_context": False,
            "visuals": profile_visuals,
            "profile_links": profile_links,
            "site_search": result,
        },
    }


def _machine_agent_intent(message: str) -> str:
    q = (message or "").lower()
    hero_name = _machine_agent_parse_hero(message)
    if any(word in q for word in ("pivot", "swap", "switch", "go to", "play if", "likely to")) and any(word in q for word in ("ban", "banned", "deny", "remove")):
        return "player_pivot"
    if _machine_agent_parse_slot_compare(message):
        return "slot_compare"
    if _machine_agent_parse_hero(message) and any(word in q for word in ("volatility", "volatile", "swing")):
        return "hero_volatility"
    if any(word in q for word in ("map", "maps", "convoy", "domination", "convergence")):
        return "map"
    if any(phrase in q for phrase in ("next pick", "likely pick", "first pick", "pick next")):
        return "next_pick"
    if any(phrase in q for phrase in ("enemy comp", "enemy comps", "full enemy", "likely full", "they still get", "can they still get", "comps can they",
                                        "what comps do", "what comp do", "what comps can", "what comp can",
                                        "comps do they", "comp do they", "comps will they", "comp will they",
                                        "comps does", "comp does", "comps get", "comp get",
                                        "comps are available", "comp is available", "available comps",
                                        "comps for them", "their comps", "their comp options")):
        return "enemy_comps"
    if any(phrase in q for phrase in ("top 4 likely bans", "top four likely bans", "likely bans after", "after our first ban", "after my first ban", "after first ban")):
        return "ban_impact"
    if hero_name and any(word in q for word in ("ban", "banned", "remove", "deny")) and any(phrase in q for phrase in ("what if", "if i", "if we", "first")):
        return "ban_impact"
    if any(phrase in q for phrase in ("ban impact", "impact of ban", "if we ban", "banning impact")) or ("banning" in q and "matter" in q):
        return "ban_impact"
    if any(phrase in q for phrase in ("pivot", "pivot prediction", "swap to")):
        return "pivot"
    if any(word in q for word in ("confidence", "explain", "explanation", "why")):
        return "confidence"
    if any(word in q for word in ("comp", "composition", "play", "run", "target")):
        return "comp"
    if any(word in q for word in ("risk", "risks", "watch", "danger", "threat", "volatile")):
        return "risk"
    if any(word in q for word in ("protect", "protects", "keep open")):
        return "protect"
    if any(word in q for word in ("comfort", "core", "hero pool", "pool")):
        return "comfort"
    if any(word in q for word in ("contested", "overlap", "shared")):
        return "contested"
    if any(word in q for word in ("stat", "stats", "win", "wr", "rate", "record", "history", "data")):
        return "stats"
    if any(word in q for word in ("force", "path", "route", "tree", "draft check", "check")):
        return "check"
    if any(word in q for word in ("ban", "bans", "counter", "deny")):
        return "ban"
    return "summary"


def _machine_agent_filter_visuals(intent: str, visuals: dict) -> dict:
    visuals = visuals or {}
    keys_by_intent = {
        "ban": ("recommended_bans", "enemy_comfort", "volatile"),
        "protect": ("recommended_protects", "target_comp", "contested"),
        "comp": ("target_comp", "our_comp_rows", "recommended_protects", "enemy_comfort"),
        "risk": ("volatile", "contested", "enemy_comfort", "recommended_bans"),
        "comfort": ("our_comfort", "enemy_comfort"),
        "contested": ("contested", "recommended_bans", "target_comp"),
        "stats": ("recommended_bans", "our_comfort", "enemy_comfort", "target_comp"),
        "slot_compare": ("slot_compare",),
        "next_pick": ("likely_next_pick", "target_comp", "enemy_comfort"),
        "enemy_comps": ("enemy_comps", "recommended_bans", "enemy_comfort", "our_comfort"),
        "ban_impact": ("hero_focus", "recommended_bans", "likely_next_pick", "target_comp", "enemy_comfort", "our_comfort", "volatile_rows"),
        "player_pivot": ("player_pivot",),
        "pivot": ("pivot_predictions", "recommended_bans"),
        "confidence": ("confidence", "target_comp", "recommended_bans"),
        "hero_volatility": ("hero_focus", "volatile_rows"),
        "check": ("recommended_bans", "recommended_protects", "target_comp", "enemy_comfort", "contested", "volatile"),
        "summary": ("recommended_bans", "target_comp", "enemy_comfort"),
    }
    selected = {key: visuals.get(key, []) for key in keys_by_intent.get(intent, ()) if visuals.get(key)}
    if intent == "map":
        selected["map_consensus"] = visuals.get("map_consensus", [])
    return selected


def _machine_agent_answer_for_intent(message: str, context_text: str, meta: dict, intent: str) -> str:
    if not meta.get("has_matchup"):
        return "Pick both teams in the agent controls and I can produce a matchup-specific draft read."

    visuals = meta.get("visuals") or {}
    ban_line = _machine_chat_join(visuals.get("recommended_bans", []), 4)
    protect_line = _machine_chat_join(visuals.get("recommended_protects", []), 3)
    comp_line = _machine_chat_join(visuals.get("target_comp", []), 6)
    enemy_line = _machine_chat_join(visuals.get("enemy_comfort", []), 4)
    our_line = _machine_chat_join(visuals.get("our_comfort", []), 4)
    next_pick = _machine_chat_join(visuals.get("likely_next_pick", []), 2)
    contested_line = _machine_chat_join(visuals.get("contested", []), 4)
    volatile_rows = visuals.get("volatile_rows", []) or []
    volatile_line = ", ".join(
        f"{row.get('hero')} ({row.get('favored_side')}, delta {row.get('delta', 0)})"
        for row in volatile_rows[:3]
        if row.get("hero")
    ) or _machine_chat_join(visuals.get("volatile", []), 3)
    enemy_comps = visuals.get("enemy_comps", []) or []
    pivot_predictions = visuals.get("pivot_predictions", []) or []
    confidence = visuals.get("confidence") or {}
    map_rows = visuals.get("map_consensus", []) or []

    lines = context_text.splitlines()
    if intent == "ban":
        ban_seq = (visuals.get("recommended_bans") or [])[:4]
        seq_line = " | ".join(f"Ban {i+1}: {h}" for i, h in enumerate(ban_seq))
        return (
            f"Top likely bans right now: {ban_line}.\n\n"
            f"{('Likely sequence: ' + seq_line + '.\\n\\n') if seq_line else ''}"
            f"If you name your first ban, I can re-rank this sequence conditionally."
        )
    elif intent == "protect":
        return (
            f"Protect {protect_line or 'our core enablers'} first.\n\n"
            f"That keeps {comp_line or 'the target comp'} live."
        )
    elif intent == "comp":
        our_comp_rows = visuals.get("our_comp_rows") or []
        if our_comp_rows:
            ranked = []
            for i, r in enumerate(our_comp_rows, 1):
                heroes = _machine_chat_join(r.get("heroes", []), 6)
                wr = r.get("win_rate", 0)
                rate = r.get("rate", 0)
                ranked.append(f"{i}. {heroes} ({wr}% WR, {rate}% pick rate)")
            comp_list = "\n".join(ranked)
            return (
                f"Best comps for us:\n{comp_list}\n\n"
                f"Lead with protects on {protect_line or 'the core anchors'} and open with {ban_line or enemy_line} on their side."
            )
        return (
            f"Lean into {comp_line or 'the strongest available route'}.\n\n"
            f"Use protects on {protect_line or 'the key anchors'} and make them answer {ban_line or enemy_line}."
        )
    elif intent == "map":
        pivot_rows = pivot_predictions
        if map_rows:
            first_row = map_rows[0] if len(map_rows) > 0 else {}
            second_row = map_rows[1] if len(map_rows) > 1 else first_row
            best_maps = _machine_chat_join(first_row.get("maps", []), 3)
            safe_maps = _machine_chat_join(second_row.get("maps", []), 3)
            pivot = pivot_rows[0] if pivot_rows else {}
            pivot_text = _machine_chat_join(pivot.get("pivot", []), 5) if pivot else "no strong pivot row"
            counter_text = _machine_chat_join(pivot.get("counter", []), 5) if pivot else "lean on the core comp path"
            return (
                f"Look for {best_maps}.\n\n"
                f"Fallback is {safe_maps}. If they pivot into {pivot_text}, answer with {counter_text}."
            )
    elif intent == "risk":
        return (
            f"The swing pieces here are {volatile_line or 'their flex threats'}.\n\n"
            f"If you want the safer draft, start with {ban_line or contested_line}."
        )
    elif intent == "comfort":
        return (
            f"Our comfort lane is {our_line or 'not clear enough yet'}.\n\n"
            f"Their comfort is {enemy_line or 'not clear enough yet'}, so draft around that overlap first."
        )
    elif intent == "contested":
        return (
            f"The real fight is over {contested_line or 'the shared core'}.\n\n"
            f"If you do not want to fight there, ban {ban_line or 'the shared core'} and keep {comp_line or 'our route'} open."
        )
    elif intent == "check":
        return (
            f"Right now I would ban {ban_line or 'their pressure pieces'} and keep {comp_line or 'our main route'} live.\n\n"
            f"Main swing check: {volatile_line or enemy_line or 'their comfort core'}."
        )
    elif intent == "stats":
        sample = confidence.get("sample", 0)
        return (
            f"On the numbers, {comp_line or 'the route'} is still best.\n\n"
            f"This read is coming from {sample} filtered records."
        )
    elif intent == "slot_compare":
        return "Use the compare view here.\n\nThe board below shows which slot actually performs better."
    elif intent == "next_pick":
        return (
            f"Next I would expect {next_pick or 'the next route anchor'}.\n\n"
            f"That keeps {comp_line or 'the route'} open and pairs with {protect_line or enemy_line}."
        )
    elif intent == "enemy_comps":
        q_lower = (message or "").lower()
        # Resolve which team is the subject — prefer an explicitly named team_b
        subject = meta.get("team_b") or "them"
        if enemy_comps:
            ranked = []
            for i, r in enumerate(enemy_comps, 1):
                heroes = _machine_chat_join(r.get("heroes", []), 6)
                wr = r.get("win_rate", 0)
                rate = r.get("rate", 0)
                ranked.append(f"{i}. {heroes} ({wr}% WR{(', ' + str(rate) + '% pick rate') if rate else ''})")
            comp_list = "\n".join(ranked)
            return (
                f"Best comp options for {subject}:\n{comp_list}\n\n"
                f"They lean on {enemy_line or 'their comfort core'}. Break it up with {ban_line or 'the first deny layer'}."
            )
        return (
            f"Read {subject}'s draft through {enemy_line or 'their comfort core'}, and keep {our_line or 'our comfort core'} available.\n\n"
            f"Use {ban_line or 'the deny layer'} to keep the full comp from settling."
        )
    elif intent == "ban_impact":
        hero = _machine_agent_parse_hero(message)
        hero_key = (hero or "").strip().lower()
        filtered_comp = _machine_chat_join(
            [item for item in visuals.get("target_comp", []) if (item or "").strip().lower() != hero_key],
            6,
        )
        filtered_followup = _machine_chat_join(
            [item for item in visuals.get("recommended_bans", []) if (item or "").strip().lower() != hero_key],
            4,
        )
        enemy_ban_line = _machine_chat_join(
            [item for item in visuals.get("recommended_bans", []) if (item or "").strip().lower() != hero_key],
            5,
        )
        ban_seq = [
            item for item in (visuals.get("recommended_bans", []) or [])
            if (item or "").strip().lower() != hero_key
        ][:4]
        seq_line = " | ".join(f"Ban {i+1}: {h}" for i, h in enumerate(ban_seq))
        next_pressure = _machine_chat_join(visuals.get("likely_next_pick", []), 2)
        return (
            f"If you first-ban {hero or 'that hero'}, their likely deny board becomes {enemy_ban_line or filtered_followup or enemy_line or 'their comfort layer'}.\n\n"
            f"{('Likely sequence: ' + seq_line + '.\n\n') if seq_line else ''}"
            f"Comfort read after that ban: they still lean on {enemy_line or 'their comfort core'}, while we can route through {our_line or filtered_comp or 'our comfort core'}.\n\n"
            f"That usually shifts the draft toward {filtered_comp or enemy_line or 'the next best route'}"
            f"{('. Next pick pressure: ' + next_pressure + '.') if next_pressure else '.'}"
        )
    elif intent == "hero_volatility":
        hero = _machine_agent_parse_hero(message)
        return (
            f"{hero or 'That hero'} is a real swing piece if it sits in {volatile_line or contested_line}.\n\n"
            f"If you want the lower-variance draft, ban {ban_line or hero or 'it'} early."
        )
    elif intent == "pivot":
        if pivot_predictions:
            row = pivot_predictions[0]
            diff = row.get("diff_count", 0) or 0
            pivot_type = "round swap" if diff <= 2 else "pressure pivot"
            pivot_label = (
                f"{diff}-hero swap — likely a situational round adjustment"
                if diff <= 2
                else f"{diff}-hero rebuild — typically a losing-map response"
            )
            return (
                f"If they start on {_machine_chat_join(row.get('base', []), 4)}, expect the pivot into {_machine_chat_join(row.get('pivot', []), 4)}.\n"
                f"Read: {pivot_label} ({pivot_type}).\n\n"
                f"Our clean answer is {_machine_chat_join(row.get('counter', []), 4) or comp_line}."
            )
        return (
            f"Watch their comfort lane: {enemy_line or 'no clean pivot read yet'}.\n\n"
            f"Keep {ban_line or comp_line} ready as the answer."
        )
    elif intent == "confidence":
        return (
            f"The model leans toward {comp_line or 'the current route'}.\n\n"
            f"Confidence is {confidence.get('confidence', 0)}% across {confidence.get('sample', 0)} records."
        )
    else:
        return (
            f"Start on {ban_line or 'their comfort core'} and hold {comp_line or 'our best route'} open.\n\n"
            f"Main pressure point is {enemy_line or volatile_line or contested_line}."
        )


def _machine_agent_site_context_text(site_result: dict) -> str:
    """Flatten a site_context search result dict into a plain text block for the LLM system prompt."""
    lines = []
    personal = site_result.get("personal_team", "")
    lines.append(f"Personal/our team: {personal}")
    for p in (site_result.get("players") or []):
        heroes = ", ".join(
            f"{h.get('hero')} ({h.get('appearances', 0)} games, {h.get('wins', 0)}-{h.get('losses', 0)})"
            for h in (p.get("heroes") or [])[:8]
        )
        lines.append(f"Player {p.get('player_name')} on {p.get('team_name', '')} ({p.get('role', '')}): {heroes}")
    for h in (site_result.get("heroes") or []):
        partners = ", ".join(r.get("partner", "") for r in (h.get("pair_partners") or [])[:5])
        lines.append(
            f"Hero {h.get('hero')} for {h.get('team')}: played {h.get('played_count', 0)}, "
            f"{h.get('played_wins', 0)}-{h.get('played_losses', 0)}, "
            f"banned {h.get('ban_count', 0)}x, protected {h.get('protect_count', 0)}x. Partners: {partners}"
        )
    for t in (site_result.get("teams") or []):
        bias = ", ".join(
            f"{b.get('hero')} (played {b.get('played_count', 0)}, banned {b.get('ban_count', 0)})"
            for b in (t.get("hero_bias") or [])[:10]
        )
        pairs = ", ".join(
            f"{r.get('hero_a')}/{r.get('hero_b')} ({r.get('co_appearances', 0)}x)"
            for r in (t.get("pair_cores") or [])[:6]
        )
        map_recs = ", ".join(
            f"{r.get('map_name')} {r.get('wins', 0)}-{r.get('losses', 0)}"
            for r in (t.get("map_stats") or [])[:6]
        )
        lines.append(f"Team {t.get('team')}: bias [{bias}]. Pairs: {pairs}. Maps: {map_recs}")
        # Include player pools
        for pp in (t.get("player_pools") or [])[:8]:
            pp_heroes = ", ".join(
                f"{h.get('hero')} ({h.get('appearances', 0)})"
                for h in (pp.get("heroes") or [])[:6]
            )
            lines.append(
                f"  - {pp.get('player_name')}{(' (' + pp.get('role') + ')') if pp.get('role') else ''}: {pp_heroes}"
            )
    for m in (site_result.get("maps") or []):
        freq = ", ".join(f"{f.get('hero')} ({f.get('count', 0)})" for f in (m.get("hero_freq") or [])[:6])
        bans = ", ".join(f"{f.get('hero')} ({f.get('count', 0)})" for f in (m.get("bans") or [])[:5])
        lines.append(
            f"Map {m.get('map_name')} for {m.get('team')}: {m.get('wins', 0)}-{m.get('losses', 0)} over {m.get('played', 0)}. "
            f"Most picked: {freq}. Bans: {bans}"
        )
    for s in (site_result.get("scrims_summary") or [])[:8]:
        maps_played = s.get("maps", []) or []
        wins = sum(1 for m in maps_played if (m.get("result") or "").lower() == "win")
        losses = sum(1 for m in maps_played if (m.get("result") or "").lower() == "loss")
        result_str = f" {wins}-{losses}" if wins + losses else ""
        lines.append(f"Scrim {s.get('scrim_date', 'undated')} vs {s.get('enemy_team') or s.get('opponent', '?')}{result_str}")
    return "\n".join(lines)


def _machine_agent_llm_answer(message: str, context: str, personal_team: str, meta: dict | None = None, intent: str = "general", site_context_text: str = "", season: str = "all") -> str | None:
    """Generate a natural-language answer using local ML (association rules + NLG). No external API calls."""
    try:
        from draft_engine.local_ml import generate_local_answer
        result = generate_local_answer(
            message=message,
            context_text=context or "",
            site_context_text=site_context_text or "",
            meta=meta or {},
            intent=intent,
            personal_team=personal_team or "",
            db=get_db(),
            season=season,
        )
        return result or None
    except Exception:
        return None


def _machine_agent_humanize_answer(text: str) -> str:
    """Strip markdown-style emphasis and inline code for plain chat rendering."""
    if not text:
        return ""
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    cleaned = cleaned.replace("`", "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _machine_agent_extract_season(message: str, fallback: str = "all") -> str:
    match = re.search(r"\b(?:season|patch|s)\s*([0-9]+)\b", message or "", re.IGNORECASE)
    if match:
        return match.group(1)
    if re.search(r"\ball\s+seasons?\b", message or "", re.IGNORECASE):
        return "all"
    return fallback or "all"


def _machine_agent_extract_map(message: str, fallback: str = "all") -> str:
    text = (message or "").lower()
    for map_name in MAPS:
        if map_name.lower() in text:
            return map_name
    return fallback or "all"


def _machine_agent_find_mentioned_teams(message: str) -> list[dict]:
    text = (message or "").lower()
    rows = get_db().execute("SELECT id, name FROM teams ORDER BY LENGTH(name) DESC").fetchall()
    matches = []
    token_set = set(re.findall(r"[a-z0-9]+", text))

    def _team_aliases(team_name: str) -> set[str]:
        cleaned = re.sub(r"[^a-z0-9\s]", "", (team_name or "").lower()).strip()
        parts = [p for p in cleaned.split() if p and p not in {"the", "team"}]
        aliases: set[str] = set()
        if not parts:
            return aliases
        aliases.add(cleaned)
        aliases.add("".join(parts))
        # Initialism: Virtus Pro -> vp, Liquid Citadel -> lc
        if len(parts) > 1:
            aliases.add("".join(p[0] for p in parts if p))
        # Common short form: Sentinels -> sen
        if parts and len(parts[0]) >= 3:
            aliases.add(parts[0][:3])
        return {a for a in aliases if len(a) >= 2}

    for row in rows:
        name = (row["name"] or "").strip()
        if not name:
            continue
        name_l = name.lower()
        if name_l in text:
            matches.append({"id": int(row["id"]), "name": name, "pos": text.find(name_l)})
            continue
        # Alias/acronym match (e.g. VP, SEN, LC, 100T)
        aliases = _team_aliases(name)
        alias_hit = next((a for a in aliases if a in token_set), None)
        if alias_hit:
            matches.append({"id": int(row["id"]), "name": name, "pos": text.find(alias_hit)})

    # Deduplicate by team id if multiple aliases hit
    dedup = {}
    for row in matches:
        if row["id"] not in dedup or row["pos"] < dedup[row["id"]]["pos"]:
            dedup[row["id"]] = row
    matches = list(dedup.values())
    matches.sort(key=lambda row: row["pos"])
    return matches


def _machine_agent_get_personal_team() -> dict | None:
    row = get_db().execute(
        "SELECT id, name FROM teams WHERE is_personal = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _machine_agent_context_from_payload(payload: dict, message: str) -> dict:
    raw_context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    personal_team = _machine_agent_get_personal_team()

    def int_or_none(value):
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    context = {
        "team_a_id": int_or_none(payload.get("team_a") or raw_context.get("team_a_id") or (personal_team or {}).get("id")),
        "team_b_id": int_or_none(payload.get("team_b") or raw_context.get("team_b_id")),
        "season": str(payload.get("season") or raw_context.get("season") or "all").strip(),
        "map": str(payload.get("map") or raw_context.get("map") or "all").strip(),
        "include_scrims": bool(payload.get("include_scrims", raw_context.get("include_scrims", True))),
        "include_tournaments": bool(payload.get("include_tournaments", raw_context.get("include_tournaments", True))),
        "reasoning_mode": "reasoning",
    }

    context["season"] = _machine_agent_extract_season(message, context["season"])
    context["map"] = _machine_agent_extract_map(message, context["map"])
    text = (message or "").lower()
    if "scrim only" in text or "only scrim" in text:
        context["include_scrims"] = True
        context["include_tournaments"] = False
    elif "tournament only" in text or "only tournament" in text:
        context["include_scrims"] = False
        context["include_tournaments"] = True

    mentioned = _machine_agent_find_mentioned_teams(message)
    if len(mentioned) >= 2:
        if not context.get("team_a_id"):
            context["team_a_id"] = mentioned[0]["id"]
            context["team_b_id"] = mentioned[1]["id"]
        else:
            opposing_mentions = [row for row in mentioned if row["id"] != context.get("team_a_id")]
            if opposing_mentions:
                context["team_b_id"] = opposing_mentions[0]["id"]
            elif len(mentioned) > 1:
                context["team_b_id"] = mentioned[1]["id"]
    elif len(mentioned) == 1:
        team = mentioned[0]
        before = text[:team["pos"]]
        if context.get("team_a_id") and context.get("team_a_id") == team["id"]:
            pass
        elif re.search(r"\b(vs|versus|against|enemy|opp|opponent)\s*$", before):
            context["team_b_id"] = team["id"]
        elif re.search(r"\b(we are|we're|our team is|as|playing as)\s*$", before):
            context["team_a_id"] = team["id"]
        elif not context.get("team_a_id") and not context.get("team_b_id"):
            context["team_a_id"] = team["id"]
        # Otherwise keep the currently selected context intact.

    for key in ("team_a_id", "team_b_id"):
        if context.get(key):
            row = get_db().execute("SELECT name FROM teams WHERE id = ?", (context[key],)).fetchone()
            context[key.replace("_id", "_name")] = row["name"] if row else ""
    return context


def _machine_agent_missing_context_response(intent: str, context: dict) -> str | None:
    if context.get("team_a_id") and context.get("team_b_id"):
        return None
    if not context.get("team_b_id"):
        if intent == "map":
            return "Which team are we drafting against, and what season or patch should I use? Example: `against Liquid Citadel season 7`."
        return "Who is the enemy team? Example: `against Virtus Pro season 7`."
    if not context.get("team_a_id"):
        return None
    return None


@app.route("/api/draft-agent", methods=["POST"])
@app.route("/api/machine-chat", methods=["POST"])
def api_machine_chat():
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Message is required."}), 400
    if len(message) > 1200:
        return jsonify({"error": "Message is too long."}), 400

    pending_message = (payload.get("pending_message") or "").strip()
    intent_message = pending_message or message
    intent = _machine_agent_intent(intent_message)
    chat_context = _machine_agent_context_from_payload(payload, message)
    reasoning_mode = "reasoning"
    season_value = chat_context.get("season") or "all"
    is_info_request = _machine_agent_is_info_request(intent_message, intent)
    # For ban_impact with a named hero but no matchup context, treat as a hero info request
    # so we use site data instead of prompting for a team.
    if intent == "ban_impact" and not chat_context.get("team_b_id") and _machine_agent_parse_hero(intent_message):
        is_info_request = True
    try:
        site_answer = _machine_agent_site_answer(intent_message, season_value)
    except Exception:
        site_answer = None
    # Reasoning mode: still allow direct site answers for explicit info asks.
    if site_answer and (not chat_context.get("team_b_id") or is_info_request):
        personal_team = (chat_context.get("team_a_name") or "") or ((_machine_agent_get_personal_team() or {}).get("name") or "")
        site_ctx = _machine_agent_site_context_text(site_answer["meta"].get("site_search") or {})
        llm_text = _machine_agent_llm_answer(
            intent_message, site_ctx, personal_team,
            meta={"has_matchup": False},
            intent=intent,
            site_context_text=site_ctx,
            season=season_value,
        )
        site_answer["meta"]["context"] = chat_context
        site_answer["meta"]["reasoning_mode"] = reasoning_mode
        site_answer["meta"]["response_engine"] = "local_ml"
        return jsonify({
            "answer": _machine_agent_humanize_answer(llm_text or site_answer["answer"]),
            "source": "site_context",
            "reason": None,
            "meta": site_answer["meta"],
        })

    follow_up = _machine_agent_missing_context_response(intent, chat_context)
    if is_info_request and not site_answer:
        return jsonify({
            "answer": _machine_agent_humanize_answer("I could not find a direct site-data match for that yet. Try naming a specific team, player, hero, map, or season (example: `Virtus Pro snapshot season 7`, `Fate player snapshot`, `Dr. Strange profile`)."),
            "source": "site_context",
            "reason": "no_entity_match",
            "meta": {
                "has_matchup": False,
                "intent": "site_info",
                "context": chat_context,
                "reasoning_mode": reasoning_mode,
                "response_engine": "local_ml",
                "needs_context": False,
                "visuals": {},
            },
        })
    if follow_up:
        return jsonify({
            "answer": follow_up,
            "source": "draft_agent",
            "reason": "missing_context",
            "meta": {
                "has_matchup": False,
                "intent": intent,
                "context": chat_context,
                "reasoning_mode": reasoning_mode,
                "response_engine": "context_prompt",
                "needs_context": True,
                "visuals": {},
            },
        })

    team_a_id = chat_context.get("team_a_id")
    team_b_id = chat_context.get("team_b_id")
    season_value = chat_context.get("season") or "all"
    selected_map_name = chat_context.get("map") or "all"
    include_scrims = bool(chat_context.get("include_scrims", True))
    include_tournaments = bool(chat_context.get("include_tournaments", True))
    if not include_scrims and not include_tournaments:
        return jsonify({"error": "Select at least one data source."}), 400

    context_text, meta = _machine_chat_build_context(
        team_a_id,
        team_b_id,
        season_value,
        selected_map_name,
        include_scrims,
        include_tournaments,
        )
    all_visuals = dict((meta.get("visuals") or {}))
    personal_team_name = meta.get("team_a") or ""
    llm_text = _machine_agent_llm_answer(
        intent_message, context_text, personal_team_name,
        meta=meta,
        intent=intent,
        site_context_text="",
        season=season_value,
    )
    answer = _machine_agent_humanize_answer(llm_text or _machine_agent_answer_for_intent(intent_message, context_text, meta, intent))
    compare_request = _machine_agent_parse_slot_compare(intent_message) if meta.get("has_matchup") else None
    player_pivot_request = _machine_agent_parse_player_pivot(intent_message, chat_context) if meta.get("has_matchup") else None
    hero_focus = _machine_agent_parse_hero(intent_message) if meta.get("has_matchup") else ""
    if intent == "player_pivot" and not player_pivot_request:
        answer = _machine_agent_player_pivot_missing_response(intent_message, chat_context)
        meta["intent"] = "player_pivot"
        meta["visuals"] = {}
    elif player_pivot_request:
        target_team_id = player_pivot_request.get("target_team_id") or team_b_id
        target_row = get_db().execute("SELECT * FROM teams WHERE id = ?", (target_team_id,)).fetchone()
        if target_row is None:
            abort(404)
        target_history = _machine_chat_filter_team_history(
            target_row,
            season_value,
            selected_map_name,
            include_scrims,
            include_tournaments,
        )
        pivot_stats = _machine_agent_player_pivot_stats(
            target_history,
            player_pivot_request["player"],
            player_pivot_request["banned_hero"],
        )
        answer = _machine_agent_player_pivot_answer(pivot_stats, target_row["name"])
        meta["intent"] = "player_pivot"
        meta["visuals"] = {
            "player_pivot": pivot_stats,
        }
    elif compare_request:
        hero_name = compare_request["hero"]
        slot_numbers = compare_request["slots"]
        db = get_db()
        team_a_row = db.execute("SELECT * FROM teams WHERE id = ?", (team_a_id,)).fetchone()
        team_b_row = db.execute("SELECT * FROM teams WHERE id = ?", (team_b_id,)).fetchone()
        team_a_stats = _machine_agent_slot_timing_stats(
            _machine_chat_filter_team_history(
                team_a_row,
                season_value,
                selected_map_name,
                include_scrims,
                include_tournaments,
            ),
            meta["team_a"],
            hero_name,
            slot_numbers,
        )
        team_b_stats = _machine_agent_slot_timing_stats(
            _machine_chat_filter_team_history(
                team_b_row,
                season_value,
                selected_map_name,
                include_scrims,
                include_tournaments,
            ),
            meta["team_b"],
            hero_name,
            slot_numbers,
        )

        def stats_line(team_stats: dict) -> str:
            slot_bits = []
            for slot in team_stats.get("slots", []):
                slot_bits.append(
                    f"{slot['label']}: {slot['maps']} maps, {slot['rate']}% of drafts, {slot['win_rate']}% WR"
                )
            leader = team_stats.get("leader") or "no clear slot edge"
            return f"{team_stats['team']} - " + "; ".join(slot_bits) + f". Edge: {leader}."

        answer = (
            f"Slot timing comparison: {hero_name}\n\n"
            + stats_line(team_a_stats)
            + "\n"
            + stats_line(team_b_stats)
        )
        meta["intent"] = "slot_compare"
        meta["visuals"] = {
            "slot_compare": {
                "hero": hero_name,
                "teams": [team_a_stats, team_b_stats],
            }
        }
    else:
        if hero_focus:
            volatile_match = next(
                (row for row in all_visuals.get("volatile_rows", []) if (row.get("hero", "") or "").lower() == hero_focus.lower()),
                None,
            )
            all_visuals["hero_focus"] = {
                "hero": hero_focus,
                "is_recommended_ban": hero_focus in all_visuals.get("recommended_bans", []),
                "in_target_comp": hero_focus in all_visuals.get("target_comp", []),
                "enemy_comfort": hero_focus in all_visuals.get("enemy_comfort", []),
                "volatile": volatile_match,
            }
        meta["intent"] = intent
        meta["visuals"] = _machine_agent_filter_visuals(intent, all_visuals)
    meta["context"] = {
        **chat_context,
        "team_a_name": meta.get("team_a") or chat_context.get("team_a_name", ""),
        "team_b_name": meta.get("team_b") or chat_context.get("team_b_name", ""),
    }
    meta["response_engine"] = "local_ml" if llm_text else "local_draft"

    return jsonify({
        "answer": answer,
        "source": "draft_agent",
        "reason": None,
        "meta": meta,
    })


@app.route("/api/draft-reasoner/enemy-scouting")
def api_draft_reasoner_enemy_scouting():
    """Return tournament loss analysis for the enemy team.

    For each map where team_b lost to another team, aggregate:
    - bans the winning team used against team_b
    - heroes the winning team played on those maps
    - heroes team_b themselves played on those losing maps (shows what didn't work)
    Only includes season-filtered data when season is specified.
    """
    db = get_db()
    team_b_id = request.args.get("team_b", type=int)
    season_value = (request.args.get("season", "") or "").strip()
    if not team_b_id:
        return jsonify({"error": "team_b is required"}), 400

    team_b_row = db.execute("SELECT * FROM teams WHERE id = ?", (team_b_id,)).fetchone()
    if team_b_row is None:
        abort(404)

    team_id = int(team_b_row["id"])
    team_name = (team_b_row["name"] or "").strip().lower()

    # Counts indexed by hero name
    ban_counts: defaultdict[str, int] = defaultdict(int)   # bans by winning teams vs team_b
    winner_hero_counts: defaultdict[str, int] = defaultdict(int)  # heroes winning teams played
    loser_hero_counts: defaultdict[str, int] = defaultdict(int)   # heroes team_b played when losing
    loss_map_total = 0
    match_summaries: list[dict] = []

    for tournament_record in TOURNAMENT_MATCHES:
        # Season filter
        rec_season = normalize_season_value(tournament_record.get("season", ""))
        if season_value and season_value.lower() not in ("all", "") and rec_season:
            if season_value != rec_season:
                continue

        # Find team_b's entry in this tournament
        team_b_entry: dict | None = None
        for t_team in tournament_record.get("tournament_teams", []):
            if not isinstance(t_team, dict):
                continue
            src_id = t_team.get("source_team_id")
            t_name = (t_team.get("name") or "").strip().lower()
            if (isinstance(src_id, int) and src_id == team_id) or (not src_id and t_name and t_name == team_name):
                team_b_entry = t_team
                break

        if team_b_entry is None:
            continue

        b_t_id = team_b_entry.get("id")

        for match in tournament_record.get("matches", []):
            if not isinstance(match, dict):
                continue

            # Determine which slot team_b occupies
            if match.get("team1_tournament_team_id") == b_t_id:
                b_slot, w_slot = "team1", "team2"
                opponent_name = (match.get("team2_name") or "").strip() or "Opponent"
            elif match.get("team2_tournament_team_id") == b_t_id:
                b_slot, w_slot = "team2", "team1"
                opponent_name = (match.get("team1_name") or "").strip() or "Opponent"
            else:
                continue

            map_losses: list[str] = []
            for map_entry in match.get("maps", []):
                if not isinstance(map_entry, dict):
                    continue

                outcome = get_map_outcome_for_slot(map_entry, b_slot)
                if outcome != "Loss":
                    continue

                loss_map_total += 1
                map_name = (map_entry.get("map_name") or map_entry.get("map") or "").strip()
                if map_name:
                    map_losses.append(map_name)

                # Winning team's bans
                draft_data = map_entry.get("draft", {})
                if isinstance(draft_data, dict):
                    w_draft = draft_data.get(w_slot, {})
                    if isinstance(w_draft, dict):
                        for slot_key, hero_val in w_draft.items():
                            if slot_key.startswith("ban") and hero_val:
                                h = _canonical_draft_hero(hero_val)
                                if h:
                                    ban_counts[h] += 1

                # Winning team's heroes played
                for section in map_entry.get("comp", []):
                    if not isinstance(section, dict):
                        continue
                    for slot in section.get(w_slot, []):
                        if isinstance(slot, dict):
                            h = _canonical_draft_hero(slot.get("hero", ""))
                            if h:
                                winner_hero_counts[h] += 1
                    # Team_b heroes on losing maps
                    for slot in section.get(b_slot, []):
                        if isinstance(slot, dict):
                            h = _canonical_draft_hero(slot.get("hero", ""))
                            if h:
                                loser_hero_counts[h] += 1

            if map_losses:
                match_summaries.append({
                    "opponent": opponent_name,
                    "maps_lost": map_losses,
                    "count": len(map_losses),
                })

    match_summaries.sort(key=lambda r: r["count"], reverse=True)

    def _to_rows(counts: dict[str, int], total_maps: int) -> list[dict]:
        rows = [
            {
                "hero": h,
                "count": c,
                "rate": round((c / total_maps) * 100, 1) if total_maps else 0.0,
            }
            for h, c in counts.items()
        ]
        rows.sort(key=lambda r: r["count"], reverse=True)
        return rows[:15]

    return jsonify({
        "team_b_name": (team_b_row["name"] or "").strip(),
        "loss_maps_total": loss_map_total,
        "winning_team_bans": _to_rows(ban_counts, loss_map_total),
        "winning_team_heroes": _to_rows(winner_hero_counts, loss_map_total),
        "enemy_heroes_when_losing": _to_rows(loser_hero_counts, loss_map_total),
        "match_summaries": match_summaries[:10],
    })


@app.route("/api/draft-reasoner/model")
def api_draft_reasoner_model():
    """Return the full matchup model plus per-team comfort/ban/comp data for the reasoner."""
    db = get_db()
    team_a_id = request.args.get("team_a", type=int)
    team_b_id = request.args.get("team_b", type=int)
    if not team_a_id or not team_b_id:
        return jsonify({"error": "Both team_a and team_b are required."}), 400

    team_rows = db.execute(
        "SELECT * FROM teams WHERE id IN (?, ?)", (team_a_id, team_b_id)
    ).fetchall()
    team_lookup = {row["id"]: row for row in team_rows}
    team_a = team_lookup.get(team_a_id)
    team_b = team_lookup.get(team_b_id)
    if team_a is None or team_b is None:
        abort(404)

    season_value = request.args.get("season", "")
    selected_map_type = get_selected_map_type(request.args.get("map_type", "all"))
    selected_map_name = (request.args.get("map", "") or "").strip()
    include_scrims = _bool_arg("include_scrims", True)
    include_tournaments = _bool_arg("include_tournaments", True)

    def _get_filtered_scrims(team_row) -> tuple[list[dict], list[dict]]:
        all_scrims = get_team_history_for_sources(
            team_row,
            include_scrims=include_scrims,
            include_tournaments=include_tournaments,
        )
        season_options = get_scrim_season_options(all_scrims)
        default_season = get_current_season_from_recent_scrim(all_scrims)
        has_unseasoned = any(not normalize_season_value(s.get("season", "")) for s in all_scrims)
        effective_default = "all" if not season_value or season_value.lower() == "all" else default_season
        selected_season = get_selected_season(
            season_value,
            season_options,
            allow_unspecified=has_unseasoned,
            default_season=effective_default,
            strict=True,
        )
        scrims = filter_scrims_by_season(all_scrims, selected_season)
        scrims = filter_scrims_by_map_type(scrims, selected_map_type)
        hero_pool_scrims = filter_scrims_by_season(all_scrims, selected_season)
        hero_pool_scrims = filter_scrims_by_map_type(hero_pool_scrims, selected_map_type)
        if selected_map_name and selected_map_name.lower() != "all":
            filtered = []
            for scrim in scrims:
                scrim_copy = dict(scrim)
                scrim_copy["maps"] = [
                    m for m in scrim.get("maps", [])
                    if ((m.get("map") or m.get("map_name") or "").strip().lower() == selected_map_name.lower())
                ]
                if scrim_copy["maps"]:
                    filtered.append(scrim_copy)
            return filtered, hero_pool_scrims
        return scrims, hero_pool_scrims

    def _get_team_roster(team_id: int) -> list[dict]:
        rows = db.execute(
            """
            SELECT name, role, main_hero, COALESCE(is_sub, 0) AS is_sub
            FROM players
            WHERE team_id = ?
              AND role NOT IN ('Coach', 'AC', 'Analyst')
            ORDER BY COALESCE(is_sub, 0), name COLLATE NOCASE
            """,
            (team_id,),
        ).fetchall()
        return [
            {
                "name": (row["name"] or "").strip(),
                "role": (row["role"] or "").strip(),
                "main_hero": normalize_hero_slot_value(row["main_hero"] or ""),
                "is_sub": bool(row["is_sub"]),
            }
            for row in rows
            if (row["name"] or "").strip()
        ]

    a_scrims, a_hero_pool_scrims = _get_filtered_scrims(team_a)
    b_scrims, b_hero_pool_scrims = _get_filtered_scrims(team_b)
    matchup = build_matchup_tree_model(
        team_a["name"],
        a_scrims,
        team_b["name"],
        b_scrims,
        team_a_hero_pool_scrims=a_hero_pool_scrims,
        team_b_hero_pool_scrims=b_hero_pool_scrims,
    )
    teams_payload = matchup.get("teams", [])

    def _build_player_hero_rows(roster: list[dict], hero_pool_scrims: list[dict]) -> list[dict]:
        rows = []
        for player in roster:
            player_name = (player.get("name") or "").strip()
            if not player_name:
                continue
            bd = build_player_hero_map_breakdown(
                player_name,
                hero_pool_scrims,
                team_slots=TEAM_SLOTS,
                canonical_draft_hero=_canonical_draft_hero,
                get_map_outcome_for_slot=get_map_outcome_for_slot,
                map_modes=MAP_MODES,
                get_map_image_url=get_map_image_url,
            )
            hero_rows = bd.get("hero_rows", [])
            total_maps = round(sum(h.get("maps", 0) for h in hero_rows), 2)
            top_heroes = [
                {
                    "hero": h["hero"],
                    "maps": h["maps"],
                    "appearances": h["maps"],
                    "usage_rate": round((h["maps"] / total_maps) * 100, 1) if total_maps else 0.0,
                    "win_rate": h.get("win_rate", 0),
                }
                for h in hero_rows
            ]
            rows.append({
                "player": player_name,
                "maps": total_maps,
                "appearances": total_maps,
                "overall_win_rate": 0.0,
                "top_heroes": top_heroes,
            })
        rows.sort(key=lambda r: (r["maps"], r["player"].lower()), reverse=True)
        return rows

    if len(teams_payload) >= 1:
        roster_a = _get_team_roster(team_a_id)
        teams_payload[0]["roster_players"] = roster_a
        teams_payload[0].setdefault("model", {})["player_hero_rows"] = _build_player_hero_rows(roster_a, a_hero_pool_scrims)
    if len(teams_payload) >= 2:
        roster_b = _get_team_roster(team_b_id)
        teams_payload[1]["roster_players"] = roster_b
        teams_payload[1].setdefault("model", {})["player_hero_rows"] = _build_player_hero_rows(roster_b, b_hero_pool_scrims)
    return jsonify(matchup)


