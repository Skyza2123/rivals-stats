import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as mod


stats = {
    "maps_touched": 0,
    "draft_hero_updates": 0,
    "comp_hero_updates": 0,
    "round_sections_filled": 0,
    "player_main_hero_updates": 0,
}


def _hero_count(section: dict) -> int:
    if not isinstance(section, dict):
        return 0

    count = 0
    for side in ("team1", "team2"):
        slots = section.get(side, [])
        if not isinstance(slots, list):
            continue
        for slot in slots:
            if isinstance(slot, dict) and str(slot.get("hero", "")).strip():
                count += 1
    return count


def _normalize_map_entry(map_entry: dict) -> bool:
    changed = False
    if not isinstance(map_entry, dict):
        return False

    draft = map_entry.get("draft", {})
    if isinstance(draft, dict):
        for side in ("team1", "team2"):
            team_draft = draft.get(side, {}) if isinstance(draft.get(side, {}), dict) else {}
            for slot_key, hero_value in list(team_draft.items()):
                old = str(hero_value or "").strip()
                new = mod.normalize_hero_slot_value(old)
                if new != old:
                    team_draft[slot_key] = new
                    stats["draft_hero_updates"] += 1
                    changed = True

    comp_sections = map_entry.get("comp", [])
    if isinstance(comp_sections, list) and comp_sections:
        for section in comp_sections:
            if not isinstance(section, dict):
                continue
            for side in ("team1", "team2"):
                slots = section.get(side, [])
                if not isinstance(slots, list):
                    continue
                for slot in slots:
                    if not isinstance(slot, dict):
                        continue
                    old = str(slot.get("hero", "")).strip()
                    new = mod.normalize_hero_slot_value(old)
                    if new != old:
                        slot["hero"] = new
                        stats["comp_hero_updates"] += 1
                        changed = True

        source_section = max((sec for sec in comp_sections if isinstance(sec, dict)), key=_hero_count, default=None)
        if source_section and _hero_count(source_section) > 0:
            source_team1 = copy.deepcopy(source_section.get("team1", []))
            source_team2 = copy.deepcopy(source_section.get("team2", []))
            for section in comp_sections:
                if not isinstance(section, dict):
                    continue
                if _hero_count(section) == 0:
                    section["team1"] = copy.deepcopy(source_team1)
                    section["team2"] = copy.deepcopy(source_team2)
                    stats["round_sections_filled"] += 1
                    changed = True

    if changed:
        stats["maps_touched"] += 1
    return changed


with mod.app.app_context():
    for scrim in mod.SCRIMS:
        if not isinstance(scrim, dict):
            continue
        mod.normalize_scrim_record(scrim)
        for map_entry in scrim.get("maps", []):
            _normalize_map_entry(map_entry)

    for tournament in mod.TOURNAMENT_MATCHES:
        if not isinstance(tournament, dict):
            continue
        mod.normalize_tournament_record(tournament)

        for map_entry in tournament.get("maps", []):
            _normalize_map_entry(map_entry)

        for tournament_match in tournament.get("matches", []):
            if not isinstance(tournament_match, dict):
                continue
            for map_entry in tournament_match.get("maps", []):
                _normalize_map_entry(map_entry)

    db = mod.get_db()

    player_rows = db.execute(
        "SELECT id, main_hero FROM players WHERE trim(coalesce(main_hero, '')) != ''"
    ).fetchall()
    for row in player_rows:
        old = str(row["main_hero"] or "").strip()
        new = mod.normalize_hero_slot_value(old)
        if new and new != old:
            db.execute("UPDATE players SET main_hero = ? WHERE id = ?", (new, row["id"]))
            stats["player_main_hero_updates"] += 1

    enemy_rows = db.execute(
        "SELECT id, main_hero FROM enemy_players WHERE trim(coalesce(main_hero, '')) != ''"
    ).fetchall()
    for row in enemy_rows:
        old = str(row["main_hero"] or "").strip()
        new = mod.normalize_hero_slot_value(old)
        if new and new != old:
            db.execute("UPDATE enemy_players SET main_hero = ? WHERE id = ?", (new, row["id"]))
            stats["player_main_hero_updates"] += 1

    db.commit()
    mod.save_app_state(allow_scrim_removal=True)

print(stats)
