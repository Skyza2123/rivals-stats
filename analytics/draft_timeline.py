# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

def opposite_team_slot(team_slot: str) -> str:
    return "team2" if team_slot == "team1" else "team1"


def _draft_slot_label(slot_key: str) -> str:
    labels = {
        "ban1": "Ban 1",
        "protect1": "Protect 1",
        "ban2": "Ban 2",
        "ban3": "Ban 3",
        "protect2": "Protect 2",
        "ban4": "Ban 4",
    }
    return labels.get(slot_key, slot_key)


def _canonical_draft_hero(raw_hero: str) -> str:
    return normalize_hero_slot_value(raw_hero)


def _summarize_draft_phase_slot_counts(slot_counts: dict[str, dict[str, int]]) -> list[dict]:
    rows = []
    for slot_key in DRAFT_SLOT_ORDER:
        slot_totals = sum(slot_counts.get(slot_key, {}).values())
        hero_rows = [
            {
                "hero": hero,
                "count": count,
                "rate": round((count / slot_totals) * 100, 1) if slot_totals else 0,
            }
            for hero, count in sorted(slot_counts.get(slot_key, {}).items(), key=lambda item: item[1], reverse=True)
        ]
        top_row = hero_rows[0] if hero_rows else None
        rows.append(
            {
                "slot_key": slot_key,
                "slot_label": _draft_slot_label(slot_key),
                "total": slot_totals,
                "top_hero": top_row["hero"] if top_row else "-",
                "top_count": top_row["count"] if top_row else 0,
                "top_rate": top_row["rate"] if top_row else 0,
                "hero_rows": hero_rows[:3],
            }
        )
    return rows


def build_draft_phase_timeline(scrims: list[dict]) -> dict:
    def new_side_counts() -> dict[str, defaultdict[str, int]]:
        return {slot_key: defaultdict(int) for slot_key in DRAFT_SLOT_ORDER}

    aggregate = {
        "main": new_side_counts(),
        "enemy": new_side_counts(),
        "maps": 0,
    }
    per_map = defaultdict(lambda: {"main": new_side_counts(), "enemy": new_side_counts(), "maps": 0})

    for scrim in scrims:
        for map_entry in scrim.get("maps", []):
            map_name = (map_entry.get("map_name", "") or "").strip() or "Unknown Map"
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"

            draft = map_entry.get("draft", {})
            if not isinstance(draft, dict):
                continue

            our_draft = draft.get(our_team_slot, {})
            enemy_draft = draft.get(opposite_team_slot(our_team_slot), {})
            if not isinstance(our_draft, dict):
                our_draft = {}
            if not isinstance(enemy_draft, dict):
                enemy_draft = {}

            aggregate["maps"] += 1
            per_map[map_name]["maps"] += 1

            for side_key, side_draft in (("main", our_draft), ("enemy", enemy_draft)):
                for slot_key in DRAFT_SLOT_ORDER:
                    hero_name = _canonical_draft_hero(side_draft.get(slot_key, ""))
                    if not hero_name:
                        continue
                    aggregate[side_key][slot_key][hero_name] += 1
                    per_map[map_name][side_key][slot_key][hero_name] += 1

    map_rows = []
    for map_name, payload in per_map.items():
        map_rows.append(
            {
                "map_name": map_name,
                "maps": payload["maps"],
                "main_rows": _summarize_draft_phase_slot_counts(payload["main"]),
                "enemy_rows": _summarize_draft_phase_slot_counts(payload["enemy"]),
            }
        )
    map_rows.sort(key=lambda row: (row["maps"], row["map_name"]), reverse=True)

    return {
        "aggregate": {
            "maps": aggregate["maps"],
            "main_rows": _summarize_draft_phase_slot_counts(aggregate["main"]),
            "enemy_rows": _summarize_draft_phase_slot_counts(aggregate["enemy"]),
        },
        "maps": map_rows,
    }


def build_draft_phase_map_comparison_rows(
    timeline_data: dict,
    compare_map_a: str,
    compare_map_b: str,
) -> list[dict]:
    map_lookup = {row["map_name"]: row for row in timeline_data.get("maps", [])}
    map_a = map_lookup.get(compare_map_a)
    map_b = map_lookup.get(compare_map_b)
    if not map_a or not map_b:
        return []

    map_a_main = {row["slot_key"]: row for row in map_a["main_rows"]}
    map_a_enemy = {row["slot_key"]: row for row in map_a["enemy_rows"]}
    map_b_main = {row["slot_key"]: row for row in map_b["main_rows"]}
    map_b_enemy = {row["slot_key"]: row for row in map_b["enemy_rows"]}

    rows = []
    for slot_key in DRAFT_SLOT_ORDER:
        rows.append(
            {
                "slot_key": slot_key,
                "slot_label": _draft_slot_label(slot_key),
                "map_a_main": map_a_main.get(slot_key, {}),
                "map_a_enemy": map_a_enemy.get(slot_key, {}),
                "map_b_main": map_b_main.get(slot_key, {}),
                "map_b_enemy": map_b_enemy.get(slot_key, {}),
            }
        )
    return rows


def filter_team_scrims_for_enemy(team_scrims: list[dict], enemy_team_id: int | None, enemy_team_name: str = "") -> list[dict]:
    if not enemy_team_id and not enemy_team_name:
        return team_scrims

    filtered = []
    enemy_name_lower = (enemy_team_name or "").strip().lower()
    for scrim in team_scrims:
        # --- ID matching ---
        if enemy_team_id:
            # Legacy enemy_team_id field (enemy_teams table).
            scrim_enemy_id = scrim.get("enemy_team_id")
            if scrim_enemy_id and scrim_enemy_id == enemy_team_id:
                filtered.append(scrim)
                continue

            # Scrims between two registered teams store IDs in team1_id / team2_id.
            # Our slot is already resolved; the opponent is on the other side.
            our_slot = scrim.get("team_slot", "team1")
            if our_slot == "team1":
                opp_id = scrim.get("team2_id")
            else:
                opp_id = scrim.get("team1_id")
            if opp_id and opp_id == enemy_team_id:
                filtered.append(scrim)
                continue

        # --- Name matching fallback ---
        scrim_enemy_name = (scrim.get("enemy_team") or scrim.get("opponent") or "").strip().lower()
        if not scrim_enemy_name:
            # Try team1_name / team2_name based on slot
            our_slot = scrim.get("team_slot", "team1")
            if our_slot == "team1":
                scrim_enemy_name = (scrim.get("team2_name") or "").strip().lower()
            else:
                scrim_enemy_name = (scrim.get("team1_name") or "").strip().lower()
        if enemy_name_lower and scrim_enemy_name == enemy_name_lower:
            filtered.append(scrim)

    return filtered


