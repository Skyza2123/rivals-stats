"""Regenerate hero_ability_details.py from DotGG Marvel Rivals API data."""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from draft_engine.hero_theory import HERO_PROFILES

API_URL = "https://api.dotgg.gg/cgfw/getgacha?game=rivals&type=heroes"
OUTPUT_FILE = Path(__file__).resolve().parent.parent / "hero_ability_details.py"

NAME_MAP = {
    "Bruce Banner": "Hulk",
    "Doctor Strange": "Dr. Strange",
    "Jeff The Land Shark": "Jeff TLS",
    "Mister Fantastic": "Mr. Fantastic",
    "Spider-man": "Spider-Man",
    "Star-lord": "Star-Lord",
    "The Punisher": "Punisher",
    "The Thing": "Thing",
}

SOURCE_PRIORITY = {
    "ultimate": 3,
    "teamup": 2,
    "ability": 1,
}

NON_ABILITY_SOURCE_TYPES = {"Weapon", "Melee", "Reload"}
IMPACT_RANK = {
    "negligible": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


def _normalize_key_text(value: str) -> str:
    return str(value or "").strip().upper().replace(" ", "")


def _source_bucket(source_type: str) -> str:
    normalized = str(source_type or "").strip().lower()
    if normalized.startswith("ultimate"):
        return "ultimate"
    if normalized.startswith("teamup"):
        return "teamup"
    return "ability"


def _normalize_source_slot(raw_keybind: str, source_bucket: str) -> str:
    normalized_key = _normalize_key_text(raw_keybind)

    if source_bucket == "teamup":
        if normalized_key == "Z":
            return "teamup_ability_1"
        if normalized_key == "X":
            return "teamup_ability_2"
        if normalized_key == "C":
            return "teamup_ability_3"
        return "teamup_ability"

    if source_bucket == "ultimate":
        return "ultimate_ability"

    if normalized_key == "E":
        return "ability_1"
    if normalized_key in {"LEFTSHIFT", "SHIFT", "LSHIFT"}:
        return "ability_2"
    if normalized_key == "F":
        return "ability_3"
    if normalized_key in {"Q"}:
        return "ultimate_ability"
    if normalized_key in {"V"}:
        return "melee_attack"
    if normalized_key in {"LEFTCLICK", "MOUSE1", "LMB"}:
        return "primary_attack"
    if normalized_key in {"RIGHTCLICK", "MOUSE2", "RMB"}:
        return "secondary_attack"
    if normalized_key == "PASSIVE":
        return "passive"
    return "ability"


def _compact_tag(tag: str) -> str:
    return tag.strip().lower().replace(" ", "")


def infer_tags(description: str, source_type: str, is_teamup: bool) -> list[str]:
    text = description.lower()
    tags = [source_type]

    source_bucket = _source_bucket(source_type)

    if is_teamup:
        tags.append("teamup")
    if source_bucket == "ultimate":
        tags.append("ultimate")

    if any(token in text for token in ("heal", "healing", "regenerate", "restor")):
        tags.append("sustain")
    if any(token in text for token in ("shield", "bonus health", "damage reduction")):
        tags.append("defensive")
    if any(token in text for token in ("stun", "disable", "bind", "slow", "ground")):
        tags.append("control")
    if any(token in text for token in ("dash", "fly", "leap", "jump", "teleport", "mobility")):
        tags.append("mobility")
    if any(token in text for token in ("revive", "revival", "resurrect", "invincibility")):
        tags.append("utility")

    return list(dict.fromkeys(_compact_tag(tag) for tag in tags if tag.strip()))


def infer_impact(description: str, source_type: str, is_teamup: bool) -> str:
    text = description.lower()
    source_bucket = _source_bucket(source_type)

    if "revive" in text or "resurrect" in text or "invincibility" in text:
        return "critical"
    if source_bucket == "ultimate":
        return "high"
    if is_teamup:
        return "high"
    if any(token in text for token in ("stun", "disable", "heal", "shield", "percentage damage")):
        return "high"
    return "medium"

def _hero_theory_teamup_names() -> set[str]:
    names: set[str] = set()
    for profile in HERO_PROFILES.values():
        raw = str(profile.get("teamup") or "").strip()
        if not raw:
            continue
        for item in raw.split(","):
            teamup_name = item.strip()
            if teamup_name:
                names.add(teamup_name)
    return names


HERO_THEORY_TEAMUP_NAMES = _hero_theory_teamup_names()

# Some team-up abilities are emitted as source_type=Normal and without Team-Up
# metadata in the upstream feed, and their ability name can differ from the
# Team-Up bonus name in hero_theory.
FORCE_TEAMUP_ABILITY_NAMES = {
    "Phoenix Warrior",
}

SKIP_DUPLICATE_NAME_SUFFIXES = (
    " fired",
)


def fetch_heroes() -> list[dict]:
    req = urllib.request.Request(
        API_URL,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://marvelrivals.gg/"},
    )
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_details(raw_heroes: list[dict]) -> dict[str, list[dict[str, object]]]:
    out: dict[str, list[dict[str, object]]] = {}

    def ensure_profile_teamups(hero_name: str, hero_rows: list[dict[str, object]]) -> list[dict[str, object]]:
        profile = HERO_PROFILES.get(hero_name, {})
        teamup_value = str(profile.get("teamup") or "").strip()
        if not teamup_value:
            return hero_rows

        existing_names = {str(item["name"]) for item in hero_rows}
        for raw_name in teamup_value.split(","):
            teamup_name = raw_name.strip()
            if not teamup_name or teamup_name in existing_names:
                continue
            existing_names.add(teamup_name)
            hero_rows.append(
                {
                    "name": teamup_name,
                    "description": f"Team-Up ability for {hero_name}: {teamup_name}.",
                    "keybind": "teamup_ability",
                    "source_type": "teamup_ability",
                    "is_teamup": True,
                    "tags": ["teamup"],
                    "impact": "high",
                }
            )

        return hero_rows

    for hero in raw_heroes:
        hero_name = NAME_MAP.get(hero["name"], hero["name"])
        merged: dict[str, dict[str, object]] = {}
        ordered_names: list[str] = []
        description_to_name: dict[str, str] = {}

        for ability in hero.get("abilities", []):
            name = " ".join(str(ability.get("name", "")).split())
            if not name:
                continue

            description = " ".join(str(ability.get("description", "")).split())
            description_key = description.strip().lower()
            raw_source_type = str(ability.get("type") or "Normal")
            is_ultimate = raw_source_type.strip().lower() == "ultimate"
            is_collab = bool(ability.get("isCollab", False))
            additional_fields = ability.get("additional_fields") or {}
            raw_keybind = ""
            if isinstance(additional_fields, dict):
                raw_keybind = str(additional_fields.get("Key") or "").strip()
            has_teamup_bonus = (
                isinstance(additional_fields, dict)
                and "Team-Up Bonus" in additional_fields
            )
            has_teamup_metadata = (
                isinstance(additional_fields, dict)
                and any(
                    "team-up" in str(key).lower() or "team up" in str(key).lower()
                    for key in additional_fields.keys()
                )
            )
            is_teamup = bool(
                is_collab
                or has_teamup_bonus
                or has_teamup_metadata
                or name in HERO_THEORY_TEAMUP_NAMES
                or name in FORCE_TEAMUP_ABILITY_NAMES
                or "blessed by khonshu" in description_key
            )
            source_bucket = "teamup" if is_teamup else ("ultimate" if is_ultimate else "ability")
            source_type = _normalize_source_slot(raw_keybind, source_bucket)
            keybind = source_type
            tags = infer_tags(description, source_type, is_teamup)
            impact = infer_impact(description, source_type, is_teamup)

            # Keep only abilities, ultimates, and team-ups.
            if raw_source_type in NON_ABILITY_SOURCE_TYPES and not is_teamup:
                continue

            # The upstream feed sometimes emits telemetry variants (e.g. "... Fired")
            # with the same description as the actual ability; collapse these.
            if description_key and description_key in description_to_name:
                canonical_name = description_to_name[description_key]
                if name.lower().endswith(SKIP_DUPLICATE_NAME_SUFFIXES):
                    current = merged.get(canonical_name)
                    if current is not None:
                        current["is_teamup"] = bool(current["is_teamup"] or is_teamup)
                        current_priority = SOURCE_PRIORITY.get(_source_bucket(str(current["source_type"])), 0)
                        new_priority = SOURCE_PRIORITY.get(_source_bucket(source_type), 0)
                        if new_priority > current_priority:
                            current["source_type"] = source_type
                        current_tags = [str(tag) for tag in current.get("tags", [])]
                        current["tags"] = list(dict.fromkeys(current_tags + tags))
                        current_impact = str(current.get("impact", "medium"))
                        if IMPACT_RANK.get(impact, 0) > IMPACT_RANK.get(current_impact, 0):
                            current["impact"] = impact
                        if len(description) > len(str(current.get("description", ""))):
                            current["description"] = description
                    continue

            if name not in merged:
                merged[name] = {
                    "name": name,
                    "description": description,
                    "keybind": keybind,
                    "source_type": source_type,
                    "is_teamup": is_teamup,
                    "tags": tags,
                    "impact": impact,
                }
                ordered_names.append(name)
                if description_key and description_key not in description_to_name:
                    description_to_name[description_key] = name
                continue

            current = merged[name]
            current["is_teamup"] = bool(current["is_teamup"] or is_teamup)

            current_priority = SOURCE_PRIORITY.get(_source_bucket(str(current["source_type"])), 0)
            new_priority = SOURCE_PRIORITY.get(_source_bucket(source_type), 0)
            if new_priority > current_priority:
                current["source_type"] = source_type

            current_tags = [str(tag) for tag in current.get("tags", [])]
            current["tags"] = list(dict.fromkeys(current_tags + tags))

            current_impact = str(current.get("impact", "medium"))
            if IMPACT_RANK.get(impact, 0) > IMPACT_RANK.get(current_impact, 0):
                current["impact"] = impact

            if len(description) > len(str(current["description"])):
                current["description"] = description

            if keybind and not str(current.get("keybind") or "").strip():
                current["keybind"] = keybind

        hero_rows = [merged[name] for name in ordered_names]

        out[hero_name] = ensure_profile_teamups(hero_name, hero_rows)

    # Deadpool aliases used in this repo.
    for alias in ("DpsPool", "Tankpool", "SupportPool"):
        out[alias] = [dict(item) for item in out.get("Deadpool", [])]
    out.pop("Deadpool", None)

    # Ensure alias heroes also include team-ups from hero_theory.
    for hero_name, hero_rows in list(out.items()):
        out[hero_name] = ensure_profile_teamups(hero_name, hero_rows)

    return out


def build_module_text(details: dict[str, list[dict[str, object]]]) -> str:
    lines: list[str] = []
    lines.append('"""Structured hero ability details sourced from the DotGG rivals feed."""')
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("HERO_ABILITY_DETAILS: dict[str, list[dict[str, object]]] = {")

    for hero_name in sorted(details):
        lines.append(f"    {hero_name!r}: [")
        for ability in details[hero_name]:
            lines.append("        {")
            lines.append(f"            'name': {ability['name']!r},")
            lines.append(f"            'description': {ability['description']!r},")
            lines.append(f"            'keybind': {ability.get('keybind', '')!r},")
            lines.append(f"            'source_type': {ability['source_type']!r},")
            lines.append(f"            'is_teamup': {ability['is_teamup']!r},")
            lines.append(f"            'tags': {ability.get('tags', [])!r},")
            lines.append(f"            'impact': {ability.get('impact', 'medium')!r},")
            lines.append("        },")
        lines.append("    ],")

    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    heroes = fetch_heroes()
    details = normalize_details(heroes)
    OUTPUT_FILE.write_text(build_module_text(details), encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE} for {len(details)} heroes")


if __name__ == "__main__":
    main()
