"""Marvel Rivals hero catalog.

This mirrors the shape of the old TypeScript hero catalog, but sources the
actual Rivals roster, roles, and strategic notes from this app's data modules.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal, TypedDict

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from data import HERO_ROLES
from draft_engine.hero_theory import HERO_ABILITIES, HERO_PROFILES
from hero_ability_details import HERO_ABILITY_DETAILS

RoleName = Literal["Vanguard", "Duelist", "Strategist"]
AbilityImpact = Literal["negligible", "low", "medium", "high", "critical"]
AbilityType = Literal[
    "ability",
    "ultimateAbility",
    "teamUpAbility",
]


class HeroAbility(TypedDict):
    label: str
    keybind: str
    name: str
    description: str
    cooldown: float
    abilityType: AbilityType
    sourceType: str
    isTeamup: bool
    tags: list[str]
    impact: AbilityImpact


class Hero(TypedDict):
    name: str
    image: str
    role: RoleName
    abilities: list[HeroAbility]


ROLE_ORDER: list[RoleName] = ["Vanguard", "Duelist", "Strategist"]

IMPACT_BY_BAN_PRIORITY: dict[str, AbilityImpact] = {
    "high": "critical",
    "medium": "high",
    "low": "medium",
}


def hero_image_filename(hero_name: str) -> str:
    """Return the static hero image filename used by this app."""
    return (
        hero_name.lower()
        .replace("&", "and")
        .replace(".", "")
        .replace("-", "_")
        .replace(" ", "_")
        + ".png"
    )


def _compact_tag(tag: str) -> str:
    return tag.strip().lower().replace(" ", "")


def _clean_text(value: str) -> str:
    try:
        value = value.encode("cp1252").decode("utf-8")
    except UnicodeError:
        pass
    return (
        value.replace(chr(8212), "-")
        .replace(chr(8211), "-")
        .replace(chr(8216), "'")
        .replace(chr(8217), "'")
        .replace(chr(8220), '"')
        .replace(chr(8221), '"')
    )


def _hero_impact(profile: dict) -> AbilityImpact:
    ban_priority = str(profile.get("ban_priority", "low")).lower()
    return IMPACT_BY_BAN_PRIORITY.get(ban_priority, "medium")


def _ability_tags(ability_type: str) -> list[str]:
    normalized = ability_type.strip().lower() or "normal"
    tags = [normalized]
    if normalized == "ultimate":
        tags.append("ultimate")
    return tags


def _ability_type(source_type: str, is_teamup: bool) -> str:
    if is_teamup:
        return "teamUpAbility"
    normalized = source_type.strip().lower() or "normal"
    if normalized == "ultimate":
        return "ultimateAbility"
    return "ability"


def _coerce_impact(value: object, default: AbilityImpact) -> AbilityImpact:
    normalized = str(value or "").strip().lower()
    if normalized in {"negligible", "low", "medium", "high", "critical"}:
        return normalized  # type: ignore[return-value]
    return default


def _normalize_keybind(keybind: str) -> str:
    return str(keybind or "").strip().upper().replace(" ", "")


def _ability_label(ability_type: str, keybind: str, index: int, is_teamup: bool) -> str:
    normalized_keybind = _normalize_keybind(keybind)
    if is_teamup or ability_type == "teamUpAbility":
        return "Team-Up"
    if ability_type == "ultimateAbility":
        return "Ultimate"
    if normalized_keybind in {"RIGHTCLICK", "MOUSE2", "RMB"}:
        return "Ability 1"
    if normalized_keybind in {"LEFTSHIFT", "SHIFT", "LSHIFT"}:
        return "Ability 2"
    if normalized_keybind in {"E"}:
        return "Ability 3"
    if normalized_keybind == "PASSIVE":
        return "Passive"
    if normalized_keybind in {"LEFTCLICK", "MOUSE1", "LMB"}:
        return "Primary Fire"
    return f"Ability {index}"


def build_hero(hero_name: str, role: RoleName) -> Hero:
    profile = HERO_PROFILES.get(hero_name, {})
    official_ability_details = HERO_ABILITY_DETAILS.get(hero_name, [])
    official_abilities = HERO_ABILITIES.get(hero_name, [])
    archetype = profile.get("archetype") or "Flex"
    sub_role = profile.get("sub_role") or ""
    comp_tags = list(profile.get("comp_tags") or [])
    impact = _hero_impact(profile)

    tags = [_compact_tag(role), _compact_tag(archetype)]
    if sub_role:
        tags.append(_compact_tag(sub_role))
    tags.extend(_compact_tag(tag) for tag in comp_tags)
    tags = list(dict.fromkeys(tags))

    abilities: list[HeroAbility] = []
    default_ability_index = 0
    if official_ability_details:
        for ability in official_ability_details:
            source_type = str(ability.get("source_type") or "Normal")
            keybind = str(ability.get("keybind") or "")
            is_teamup = bool(ability.get("is_teamup", False))
            ability_type = _ability_type(source_type, is_teamup)
            normalized_keybind = _normalize_keybind(keybind)
            if ability_type == "ability" and normalized_keybind not in {
                "PASSIVE",
                "LEFTCLICK",
                "MOUSE1",
                "LMB",
                "",
            }:
                default_ability_index += 1
            detail_tags = [
                _compact_tag(str(tag))
                for tag in list(ability.get("tags") or [])
                if str(tag).strip()
            ]
            merged_tags = list(
                dict.fromkeys(
                    tags + detail_tags + _ability_tags(source_type) + (["teamup"] if is_teamup else [])
                )
            )
            abilities.append(
                {
                    "label": _ability_label(ability_type, keybind, default_ability_index, is_teamup),
                    "keybind": keybind,
                    "name": str(ability["name"]),
                    "description": _clean_text(str(ability["description"])),
                    "cooldown": 0,
                    "abilityType": ability_type,
                    "sourceType": source_type,
                    "isTeamup": is_teamup,
                    "tags": merged_tags,
                    "impact": _coerce_impact(ability.get("impact"), impact),
                }
            )
    else:
        ability_names = list(dict.fromkeys(official_abilities))
        if not ability_names:
            ability_names = [f"{hero_name} Ability"]
        for name in ability_names:
            default_ability_index += 1
            abilities.append(
                {
                    "label": _ability_label("ability", "", default_ability_index, False),
                    "keybind": "",
                    "name": name,
                    "description": _clean_text(f"{hero_name} ability: {name}."),
                    "cooldown": 0,
                    "abilityType": "ability",
                    "sourceType": "Unknown",
                    "isTeamup": False,
                    "tags": tags + ["ability"],
                    "impact": impact,
                }
            )

    # Align with hero theory: ensure recorded Team-Up names exist as Team-Up abilities.
    teamup_value = str(profile.get("teamup") or "").strip()
    if teamup_value:
        known_teamups = {ability["name"] for ability in abilities if ability["isTeamup"]}
        for raw_name in teamup_value.split(","):
            teamup_name = raw_name.strip()
            if not teamup_name or teamup_name in known_teamups:
                continue
            known_teamups.add(teamup_name)
            abilities.append(
                {
                    "label": _ability_label("teamUpAbility", "", default_ability_index, True),
                    "keybind": "",
                    "name": teamup_name,
                    "description": _clean_text(
                        f"Team-Up ability for {hero_name}: {teamup_name}."
                    ),
                    "cooldown": 0,
                    "abilityType": "teamUpAbility",
                    "sourceType": "HeroTheory",
                    "isTeamup": True,
                    "tags": tags + ["teamup"],
                    "impact": impact,
                }
            )

    return {
        "name": hero_name,
        "image": hero_image_filename(hero_name),
        "role": role,
        "abilities": abilities,
    }


role_hero_mapping: dict[RoleName, list[str]] = {
    role: sorted(HERO_ROLES.get(role, []))
    for role in ROLE_ORDER
}

all_heroes: list[Hero] = [
    build_hero(hero_name, role)
    for role in ROLE_ORDER
    for hero_name in role_hero_mapping[role]
]

hero_role_mapping: dict[str, RoleName] = {
    hero_name: role
    for role, hero_names in role_hero_mapping.items()
    for hero_name in hero_names
}

subrole_hero_mapping: dict[str, list[str]] = {}
for hero in all_heroes:
    profile = HERO_PROFILES.get(hero["name"], {})
    archetype = profile.get("archetype") or "Flex"
    subrole = f"{archetype}{hero['role']}"
    subrole_hero_mapping.setdefault(subrole, []).append(hero["name"])

SUBROLE_DISPLAY_NAMES: dict[str, str] = {
    key: key.replace("Vanguard", " Vanguard")
    .replace("Duelist", " Duelist")
    .replace("Strategist", " Strategist")
    for key in subrole_hero_mapping
}

SUBROLE_ORDER: list[str] = list(subrole_hero_mapping)

ROLE_SUBROLES: dict[RoleName, list[str]] = {
    role: [subrole for subrole in SUBROLE_ORDER if subrole.endswith(role)]
    for role in ROLE_ORDER
}

hero_priority: dict[RoleName, int] = {
    "Vanguard": 1,
    "Duelist": 2,
    "Strategist": 3,
}

hero_ability_mapping: dict[str, dict[str, object]] = {
    hero["name"]: {
        "abilityNames": [ability["name"] for ability in hero["abilities"]],
        "ultimateNames": [
            ability["name"]
            for ability in hero["abilities"]
            if ability["abilityType"] == "ultimateAbility"
        ],
    }
    for hero in all_heroes
}

HEROES = all_heroes
HERO_ROLE_MAPPING = hero_role_mapping
ROLE_HERO_MAPPING = role_hero_mapping
SUBROLE_HERO_MAPPING = subrole_hero_mapping
HERO_PRIORITY = hero_priority
HERO_ABILITY_MAPPING = hero_ability_mapping


def get_hero_subrole(hero: str, role: RoleName) -> str | None:
    for subrole in ROLE_SUBROLES[role]:
        if hero in subrole_hero_mapping[subrole]:
            return subrole
    return None


def get_hero_role(hero: str) -> RoleName:
    return hero_role_mapping.get(hero, "Duelist")
