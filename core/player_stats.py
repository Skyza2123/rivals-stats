# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def compute_player_stats(player_name: str, scrims: list[dict] | None = None) -> dict:
    target = player_name.strip()
    if not target:
        return {
            "maps_played": 0,
            "wins": 0,
            "losses": 0,
            "events_mentioned": 0,
            "first_kills": 0,
            "first_deaths": 0,
            "first_kill_fight_wins": 0,
            "first_kill_fight_losses": 0,
            "first_death_fight_wins": 0,
            "first_death_fight_losses": 0,
            "top_first_kill_victim_hero": "",
            "top_first_death_killer_hero": "",
            "win_rate": 0,
        }

    maps_played = 0
    wins = 0
    losses = 0
    unresolved_maps = 0
    unresolved_map_refs: list[dict] = []
    events_mentioned = 0
    first_kills = 0
    first_deaths = 0
    first_kill_fight_wins = 0
    first_kill_fight_losses = 0
    first_death_fight_wins = 0
    first_death_fight_losses = 0
    first_kill_victim_heroes: Counter[str] = Counter()
    first_death_killer_heroes: Counter[str] = Counter()
    target_lower = target.lower()
    exact_name_pattern = re.compile(r"(?<!\\w)" + re.escape(target_lower) + r"(?!\\w)")
    source_scrims = scrims if scrims is not None else SCRIMS

    for scrim in source_scrims:
        for map_entry in scrim["maps"]:
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            player_found = False
            for section in map_entry.get("comp", []):
                for slot in section.get(our_team_slot, []):
                    if slot.get("player", "").strip().lower() == target_lower:
                        player_found = True
                        break
                if player_found:
                    break

            if player_found:
                maps_played += 1
                result = get_map_outcome_for_slot(map_entry, our_team_slot)
                if result == "Win":
                    wins += 1
                elif result == "Loss":
                    losses += 1
                else:
                    unresolved_maps += 1
                    unresolved_map_refs.append({"scrim_id": scrim.get("id"), "map_id": map_entry.get("id")})

            for event in map_entry.get("events", []):
                description = event.get("description", "").strip().lower()
                if exact_name_pattern.search(description):
                    events_mentioned += 1
                killer_player = (event.get("first_kill_player") or event.get("killer_player") or "").strip().lower()
                victim_player = (event.get("first_death_player") or event.get("victim_player") or "").strip().lower()
                event_type = (event.get("event_type") or "").strip()

                if killer_player == target_lower and event_type in {"Fight", "First Kill", "Pick"}:
                    first_kills += 1
                    victim_hero = _canonical_draft_hero(event.get("first_death_hero") or event.get("victim_hero") or "")
                    if victim_hero:
                        first_kill_victim_heroes[victim_hero] += 1
                    if event_type == "Fight":
                        fight_winner = (event.get("fight_winner") or "").strip()
                        if fight_winner == our_team_slot:
                            first_kill_fight_wins += 1
                        elif fight_winner:
                            first_kill_fight_losses += 1

                if victim_player == target_lower and event_type in {"Fight", "First Death", "Death"}:
                    first_deaths += 1
                    killer_hero = _canonical_draft_hero(event.get("first_kill_hero") or event.get("killer_hero") or "")
                    if killer_hero:
                        first_death_killer_heroes[killer_hero] += 1
                    if event_type == "Fight":
                        fight_winner = (event.get("fight_winner") or "").strip()
                        if fight_winner == our_team_slot:
                            first_death_fight_wins += 1
                        elif fight_winner:
                            first_death_fight_losses += 1

    decided_maps = wins + losses
    win_rate = round((wins / decided_maps) * 100, 1) if decided_maps else 0
    top_first_kill_victim_hero = first_kill_victim_heroes.most_common(1)[0][0] if first_kill_victim_heroes else ""
    top_first_death_killer_hero = first_death_killer_heroes.most_common(1)[0][0] if first_death_killer_heroes else ""

    return {
        "maps_played": maps_played,
        "decided_maps": decided_maps,
        "unresolved_maps": unresolved_maps,
        "unresolved_map_refs": unresolved_map_refs,
        "wins": wins,
        "losses": losses,
        "events_mentioned": events_mentioned,
        "first_kills": first_kills,
        "first_deaths": first_deaths,
        "first_kill_fight_wins": first_kill_fight_wins,
        "first_kill_fight_losses": first_kill_fight_losses,
        "first_death_fight_wins": first_death_fight_wins,
        "first_death_fight_losses": first_death_fight_losses,
        "top_first_kill_victim_hero": top_first_kill_victim_hero,
        "top_first_death_killer_hero": top_first_death_killer_hero,
        "win_rate": win_rate,
    }


def build_player_recent_maps(player_name: str, scrims: list[dict], *, limit: int = 15) -> list[dict]:
    target_name = (player_name or "").strip().lower()
    if not target_name:
        return []

    rows: list[dict] = []
    for scrim in scrims:
        scrim_date = (scrim.get("scrim_date") or "").strip()
        # enemy_team/opponent always identifies the actual opponent regardless of
        # which team slot our team occupies, so use it directly.
        opponent_name = str(scrim.get("enemy_team") or scrim.get("opponent") or "").strip() or "Enemy Team"

        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            heroes: set[str] = set()
            found = False
            for section in map_entry.get("comp", []):
                for slot in section.get(our_team_slot, []):
                    if (slot.get("player", "") or "").strip().lower() != target_name:
                        continue
                    found = True
                    hero_name = _canonical_draft_hero(slot.get("hero", ""))
                    if hero_name:
                        heroes.add(hero_name)

            if not found:
                continue

            rows.append(
                {
                    "scrim_id": scrim.get("id"),
                    "scrim_date": scrim_date,
                    "map_name": (map_entry.get("map_name") or "").strip(),
                    "result": get_map_outcome_for_slot(map_entry, our_team_slot),
                    "opponent": opponent_name,
                    "heroes": sorted(heroes),
                }
            )

    rows.sort(
        key=lambda row: (
            row.get("scrim_date", ""),
            int(row.get("scrim_id") or 0),
        ),
        reverse=True,
    )
    return rows[: max(1, int(limit or 1))]


def build_player_submap_swap_summary(player_name: str, scrims: list[dict], *, limit: int = 20) -> dict:
    target_name = (player_name or "").strip().lower()
    if not target_name:
        return {"swap_count": 0, "transition_count": 0, "swap_rate": 0, "swap_events": []}

    transitions = 0
    swaps = 0
    swap_events: list[dict] = []

    for scrim in scrims:
        scrim_date = (scrim.get("scrim_date") or "").strip()
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            section_rows: list[dict] = []
            for idx, section in enumerate(map_entry.get("comp", [])):
                heroes: set[str] = set()
                for slot in section.get(our_team_slot, []):
                    if (slot.get("player", "") or "").strip().lower() != target_name:
                        continue
                    hero_name = _canonical_draft_hero(slot.get("hero", ""))
                    if hero_name:
                        heroes.add(hero_name)
                if not heroes:
                    continue
                section_rows.append(
                    {
                        "label": (section.get("submap") or f"Round {idx + 1}").strip(),
                        "heroes": sorted(heroes),
                    }
                )

            if len(section_rows) < 2:
                continue

            for prev, curr in zip(section_rows, section_rows[1:]):
                transitions += 1
                if prev["heroes"] == curr["heroes"]:
                    continue
                swaps += 1
                swap_events.append(
                    {
                        "scrim_id": scrim.get("id"),
                        "scrim_date": scrim_date,
                        "map_name": (map_entry.get("map_name") or "").strip(),
                        "from_label": prev["label"],
                        "to_label": curr["label"],
                        "from_heroes": prev["heroes"],
                        "to_heroes": curr["heroes"],
                    }
                )

    swap_events.sort(
        key=lambda row: (
            row.get("scrim_date", ""),
            int(row.get("scrim_id") or 0),
        ),
        reverse=True,
    )

    return {
        "swap_count": swaps,
        "transition_count": transitions,
        "swap_rate": round((swaps / transitions) * 100, 1) if transitions else 0,
        "swap_events": swap_events[: max(1, int(limit or 1))],
    }


def build_player_ban_impact(player_name: str, scrims: list[dict]) -> list[dict]:
    """Return ban impact rows for every hero in the player's pool.

    Each row contains:
      hero, hero_maps, hero_wr, times_banned, wr_when_banned, wr_delta, all_pivots, top_pivot
    """
    target_name = (player_name or "").strip().lower()
    if not target_name:
        return []

    # First pass: count how many maps the player appeared on each hero.
    hero_total_maps: dict[str, int] = defaultdict(int)
    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            seen_heroes: set[str] = set()
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(our_team_slot, []):
                    if (slot.get("player", "") or "").strip().lower() != target_name:
                        continue
                    h = _canonical_draft_hero(slot.get("hero", ""))
                    if h and h not in seen_heroes:
                        hero_total_maps[h] += 1
                        seen_heroes.add(h)

    if not hero_total_maps:
        return []

    # Track heroes with enough play time to be meaningful (>=5 maps), sorted by most played.
    all_heroes = sorted(
        (h for h in hero_total_maps if hero_total_maps[h] >= 5),
        key=lambda h: hero_total_maps[h], reverse=True,
    )
    if not all_heroes:
        return []

    # Second pass: for each map check enemy bans, outcome, and player heroes played.
    times_banned: dict[str, int] = defaultdict(int)
    avail_wins: dict[str, int] = defaultdict(int)
    avail_losses: dict[str, int] = defaultdict(int)
    ban_wins: dict[str, int] = defaultdict(int)
    ban_losses: dict[str, int] = defaultdict(int)
    # pivot_stats only meaningful for heroes with >=2 maps (likely targets)
    pivot_stats: dict[str, dict] = {h: defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0}) for h in all_heroes}

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            result = get_map_outcome_for_slot(map_entry, our_team_slot)

            enemy_slot = "team2" if our_team_slot == "team1" else "team1"
            draft = map_entry.get("draft", {})
            enemy_draft = draft.get(enemy_slot, {}) if isinstance(draft, dict) else {}
            enemy_bans = {
                _canonical_draft_hero(v)
                for k, v in enemy_draft.items()
                if "ban" in k and _canonical_draft_hero(v)
            }

            player_hero_counts: Counter[str] = Counter()
            player_hero_first_seen: dict[str, int] = {}
            player_found = False
            for section_index, section in enumerate(map_entry.get("comp", [])):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(our_team_slot, []):
                    if (slot.get("player", "") or "").strip().lower() != target_name:
                        continue
                    player_found = True
                    h = _canonical_draft_hero(slot.get("hero", ""))
                    if h:
                        player_hero_counts[h] += 1
                        player_hero_first_seen.setdefault(h, section_index)

            if not player_found:
                continue
            player_heroes = set(player_hero_counts)

            for hero_h in all_heroes:
                if hero_h in enemy_bans:
                    times_banned[hero_h] += 1
                    if result == "Win":
                        ban_wins[hero_h] += 1
                    elif result == "Loss":
                        ban_losses[hero_h] += 1
                    pivot_candidates = [
                        (h, count)
                        for h, count in player_hero_counts.items()
                        if h != hero_h
                    ]
                    if pivot_candidates:
                        pivot_candidates.sort(
                            key=lambda item: (-item[1], player_hero_first_seen.get(item[0], 9999), item[0])
                        )
                        pivot_hero = pivot_candidates[0][0]
                        pivot_stats[hero_h][pivot_hero]["count"] += 1
                        if result == "Win":
                            pivot_stats[hero_h][pivot_hero]["wins"] += 1
                        elif result == "Loss":
                            pivot_stats[hero_h][pivot_hero]["losses"] += 1
                elif hero_h in player_heroes:
                    if result == "Win":
                        avail_wins[hero_h] += 1
                    elif result == "Loss":
                        avail_losses[hero_h] += 1

    rows = []
    for hero_h in all_heroes:
        a_w = avail_wins[hero_h]
        a_l = avail_losses[hero_h]
        a_dec = a_w + a_l
        hero_wr: float | None = round((a_w / a_dec) * 100, 1) if a_dec else None

        b_w = ban_wins[hero_h]
        b_l = ban_losses[hero_h]
        b_dec = b_w + b_l
        wr_banned: float | None = round((b_w / b_dec) * 100, 1) if b_dec else None

        delta: float | None = round(wr_banned - hero_wr, 1) if (hero_wr is not None and wr_banned is not None) else None

        pvts = []
        for pvt_h, s in pivot_stats[hero_h].items():
            pvt_dec = s["wins"] + s["losses"]
            pvt_wr: float | None = round((s["wins"] / pvt_dec) * 100, 1) if pvt_dec else None
            pvts.append({"hero": pvt_h, "count": s["count"], "wr": pvt_wr})
        pvts.sort(key=lambda x: x["count"], reverse=True)

        rows.append({
            "hero": hero_h,
            "hero_maps": hero_total_maps.get(hero_h, 0),
            "hero_wr": hero_wr,
            "times_banned": times_banned[hero_h],
            "wr_when_banned": wr_banned,
            "wr_delta": delta,
            "all_pivots": pvts,
            "top_pivot": pvts[0] if pvts else None,
        })

    # Sort: heroes that are actually banned first (by ban count), then by maps played
    rows.sort(key=lambda r: (r["times_banned"], r["hero_maps"]), reverse=True)
    return rows


def build_team_ban_impact(scrims: list[dict]) -> list[dict]:
    """Return ban impact rows for every hero in the team's pool with >=5 maps played.

    Each row contains:
      hero, hero_maps, hero_wr, times_banned, wr_when_banned, wr_delta, all_pivots, top_pivot
    """
    def _ban_impact_hero_role(hero_name: str) -> str:
        hero_key = _compact_text(_canonical_draft_hero(hero_name) or hero_name)
        if not hero_key:
            return ""
        for role_name, role_heroes in HERO_ROLES.items():
            if any(_compact_text(role_hero) == hero_key for role_hero in role_heroes):
                return role_name
        return ""

    # First pass: count how many maps each hero was played by this team.
    hero_total_maps: dict[str, int] = defaultdict(int)
    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            seen_heroes: set[str] = set()
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(our_team_slot, []):
                    h = _canonical_draft_hero(slot.get("hero", ""))
                    if h and h not in seen_heroes:
                        hero_total_maps[h] += 1
                        seen_heroes.add(h)

    if not hero_total_maps:
        return []

    all_heroes = sorted(
        (h for h in hero_total_maps if hero_total_maps[h] >= 5),
        key=lambda h: hero_total_maps[h], reverse=True,
    )
    if not all_heroes:
        return []

    times_banned: dict[str, int] = defaultdict(int)
    avail_wins: dict[str, int] = defaultdict(int)
    avail_losses: dict[str, int] = defaultdict(int)
    ban_wins: dict[str, int] = defaultdict(int)
    ban_losses: dict[str, int] = defaultdict(int)
    pivot_stats: dict[str, dict] = {h: defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0}) for h in all_heroes}
    hero_roles_by_hero = {hero: _ban_impact_hero_role(hero) for hero in all_heroes}

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            result = get_map_outcome_for_slot(map_entry, our_team_slot)

            enemy_slot = "team2" if our_team_slot == "team1" else "team1"
            draft = map_entry.get("draft", {})
            enemy_draft = draft.get(enemy_slot, {}) if isinstance(draft, dict) else {}
            enemy_bans = {
                _canonical_draft_hero(v)
                for k, v in enemy_draft.items()
                if "ban" in k and _canonical_draft_hero(v)
            }

            team_heroes: set[str] = set()
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(our_team_slot, []):
                    h = _canonical_draft_hero(slot.get("hero", ""))
                    if h:
                        team_heroes.add(h)

            if not team_heroes:
                continue

            for hero_h in all_heroes:
                if hero_h in enemy_bans:
                    banned_hero_role = hero_roles_by_hero.get(hero_h, "")
                    times_banned[hero_h] += 1
                    if result == "Win":
                        ban_wins[hero_h] += 1
                    elif result == "Loss":
                        ban_losses[hero_h] += 1
                    for h in team_heroes:
                        if h != hero_h and _ban_impact_hero_role(h) == banned_hero_role:
                            pivot_stats[hero_h][h]["count"] += 1
                            if result == "Win":
                                pivot_stats[hero_h][h]["wins"] += 1
                            elif result == "Loss":
                                pivot_stats[hero_h][h]["losses"] += 1
                elif hero_h in team_heroes:
                    if result == "Win":
                        avail_wins[hero_h] += 1
                    elif result == "Loss":
                        avail_losses[hero_h] += 1

    rows = []
    for hero_h in all_heroes:
        a_w = avail_wins[hero_h]
        a_l = avail_losses[hero_h]
        a_dec = a_w + a_l
        hero_wr: float | None = round((a_w / a_dec) * 100, 1) if a_dec else None

        b_w = ban_wins[hero_h]
        b_l = ban_losses[hero_h]
        b_dec = b_w + b_l
        wr_banned: float | None = round((b_w / b_dec) * 100, 1) if b_dec else None

        delta: float | None = round(wr_banned - hero_wr, 1) if (hero_wr is not None and wr_banned is not None) else None

        pvts = []
        for pvt_h, s in pivot_stats[hero_h].items():
            pvt_dec = s["wins"] + s["losses"]
            pvt_wr: float | None = round((s["wins"] / pvt_dec) * 100, 1) if pvt_dec else None
            pvts.append({"hero": pvt_h, "count": s["count"], "wr": pvt_wr})
        pvts.sort(key=lambda x: x["count"], reverse=True)

        rows.append({
            "hero": hero_h,
            "hero_maps": hero_total_maps.get(hero_h, 0),
            "hero_wr": hero_wr,
            "times_banned": times_banned[hero_h],
            "wr_when_banned": wr_banned,
            "wr_delta": delta,
            "all_pivots": pvts,
            "top_pivot": pvts[0] if pvts else None,
        })

    rows.sort(key=lambda r: (r["times_banned"], r["hero_maps"]), reverse=True)
    return rows


def build_team_tournament_scrims(team_row: sqlite3.Row | dict) -> list[dict]:
    team_id = int(team_row["id"])
    team_name = (team_row["name"] or "").strip()
    tournament_scrims: list[dict] = []

    for tournament_record in TOURNAMENT_MATCHES:
        for tournament_team in tournament_record.get("tournament_teams", []):
            if not isinstance(tournament_team, dict):
                continue

            source_team_id = tournament_team.get("source_team_id")
            tournament_team_name = (tournament_team.get("name") or "").strip()
            matches_team = (
                (isinstance(source_team_id, int) and source_team_id == team_id)
                or (
                    not source_team_id
                    and tournament_team_name
                    and (
                        tournament_team_name.lower() == team_name.lower()
                        or _team_names_match(tournament_team_name, team_name)
                    )
                )
            )
            if not matches_team:
                continue

            tournament_scrims.extend(build_tournament_team_scrims(tournament_record, tournament_team))

    return tournament_scrims


def build_team_tournament_rows(team_row: sqlite3.Row | dict) -> list[dict]:
    team_id = int(team_row["id"])
    team_name = (team_row["name"] or "").strip()
    rows: list[dict] = []

    for tournament_record in TOURNAMENT_MATCHES:
        selected_tournament_team: dict | None = None
        for tournament_team in tournament_record.get("tournament_teams", []):
            if not isinstance(tournament_team, dict):
                continue

            source_team_id = tournament_team.get("source_team_id")
            tournament_team_name = (tournament_team.get("name") or "").strip()
            matches_team = (
                (isinstance(source_team_id, int) and source_team_id == team_id)
                or (
                    not source_team_id
                    and tournament_team_name
                    and (
                        tournament_team_name.lower() == team_name.lower()
                        or _team_names_match(tournament_team_name, team_name)
                    )
                )
            )
            if matches_team:
                selected_tournament_team = tournament_team
                break

        if selected_tournament_team is None:
            continue

        team_scrims = build_tournament_team_scrims(tournament_record, selected_tournament_team)
        analytics = build_scrim_analytics(team_scrims)
        rows.append(
            {
                "tournament_id": tournament_record.get("id"),
                "tournament_name": tournament_record.get("tournament_name") or "Tournament",
                "scrim_date": tournament_record.get("scrim_date", ""),
                "season": tournament_record.get("season", ""),
                "maps": analytics["summary"].get("total_maps", 0),
                "wins": analytics["summary"].get("total_wins", 0),
                "losses": analytics["summary"].get("total_losses", 0),
                "win_rate": analytics["summary"].get("overall_win_rate", 0),
                "tournament_team_id": selected_tournament_team.get("id"),
            }
        )

    rows.sort(key=lambda row: ((row.get("scrim_date") or ""), int(row.get("tournament_id") or 0)), reverse=True)
    return rows


