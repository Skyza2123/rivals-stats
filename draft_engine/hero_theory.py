"""
hero_theory.py — Marvel Rivals hero knowledge base.

Answers "WHAT HEROES DO" — not how the agent reasons.

DESIGN INTENT:
  Keep entries factual and brief. One archetype, one function sentence, tag lists.
  The LLM receives this as targeted context injection, not as part of the persona block.
  Do NOT embed reasoning instructions here. Those live in llm.py.
  Add new heroes each season in hero_theory_data.json. This module loads,
  validates, and exposes that data through small helper functions.
"""
from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Canonical data
# ---------------------------------------------------------------------------
def _load_json_dict(filename: str) -> dict:
    return json.loads(Path(__file__).with_name(filename).read_text(encoding="utf-8"))


HERO_THEORY_DATA: dict[str, object] = _load_json_dict("hero_theory_data.json")
ROLES: dict[str, str] = HERO_THEORY_DATA["roles"]
ARCHETYPES: dict[str, str] = HERO_THEORY_DATA["archetypes"]
SUBROLES: dict[str, str] = HERO_THEORY_DATA["subroles"]
HERO_PROFILES: dict[str, dict] = {
    hero_name: hero_data.get("profile", {})
    for hero_name, hero_data in HERO_THEORY_DATA["heroes"].items()
}
HERO_GAME_DATA: dict[str, dict] = {
    hero_name: hero_data.get("game_data", {})
    for hero_name, hero_data in HERO_THEORY_DATA["heroes"].items()
}
HERO_ABILITIES: dict[str, dict] = {
    hero_name: hero_data.get("abilities", {})
    for hero_name, hero_data in HERO_THEORY_DATA["heroes"].items()
}
HERO_KITS: dict[str, dict] = {
    hero_name: hero_data.get("kit", {})
    for hero_name, hero_data in HERO_THEORY_DATA["heroes"].items()
}
ABILITY_IMPACT_TIERS = {"critical", "high", "medium", "low", "negligible"}

# ---------------------------------------------------------------------------
# Comp archetypes
# ---------------------------------------------------------------------------
COMP_ARCHETYPES: dict[str, dict] = HERO_THEORY_DATA["comp_archetypes"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HERO_GAME_DATA_ALIASES: dict[str, str] = HERO_THEORY_DATA.get("aliases", {})


def _resolve_hero_name(hero_name: str, source: dict[str, dict]) -> str | None:
    key = (hero_name or "").strip()
    if key in source:
        return key

    lower = key.lower()
    alias = _HERO_GAME_DATA_ALIASES.get(lower)
    if alias in source:
        return alias

    for name in source:
        if name.lower() == lower:
            return name
    return None


def _style_key(style: str) -> str:
    return (style or "").strip().lower().replace(" ", "_")


def get_hero_game_data(hero_name: str) -> dict | None:
    """Return source roster/game data from heroes.json (case-insensitive), or None."""
    resolved = _resolve_hero_name(hero_name, HERO_GAME_DATA)
    if not resolved:
        return None
    return HERO_GAME_DATA[resolved]


def _combine_hero_theory_record(hero_name: str) -> dict:
    game_data = get_hero_game_data(hero_name) or {}
    profile = HERO_PROFILES.get(hero_name, {})
    scores = HERO_SCORES.get(hero_name, {})
    ability_data = HERO_ABILITIES.get(hero_name, {})
    kit = HERO_KITS.get(hero_name, {})

    combined = {"name": hero_name, **profile}
    if game_data:
        combined["game_data"] = game_data
        for field in (
            "source_id",
            "source_name",
            "real_name",
            "image_url",
            "attack_type",
            "difficulty",
            "teams",
            "transformations",
            "abilities",
            "teamup_abilities",
        ):
            if field in game_data:
                combined.setdefault(field, game_data[field])

    if scores:
        combined["scores"] = scores
        for field, value in scores.items():
            combined.setdefault(field, value)

    if ability_data:
        combined["ability_data"] = ability_data

    if kit:
        combined["kit"] = kit

    return combined


def get_hero_profile(hero_name: str) -> dict | None:
    """Return the combined theory/profile/game-data record for a hero."""
    resolved = _resolve_hero_name(hero_name, HERO_THEORY)
    if not resolved:
        return None
    return dict(HERO_THEORY[resolved])


def get_comp_tags(heroes: list[str]) -> list[str]:
    """
    Given a hero list, return the top-2 comp style tags supported by the group.
    """
    from collections import Counter
    counts: Counter = Counter()
    for h in heroes:
        p = get_hero_profile(h)
        if p:
            for tag in p.get("comp_tags", []):
                counts[tag] += 1
    return [tag for tag, _ in counts.most_common(2)]


def describe_hero(hero_name: str) -> str:
    """One-line description of a hero for prompt injection."""
    p = get_hero_profile(hero_name)
    if not p:
        return f"{hero_name}: no profile available."
    roster_details = []
    if p.get("attack_type"):
        roster_details.append(str(p["attack_type"]))
    if p.get("difficulty"):
        roster_details.append(f"difficulty {p['difficulty']}")
    detail_suffix = f" {'; '.join(roster_details)}." if roster_details else ""
    return (
        f"{p.get('name', hero_name)} ({p['role']} / {p['archetype']}): {p['function']} "
        f"Ban priority: {p['ban_priority']}.{detail_suffix}"
    )


def describe_comp(heroes: list[str]) -> str:
    """
    Given a hero lineup, infer the comp archetype and return a brief description.
    """
    tags = get_comp_tags(heroes)
    if not tags:
        return "Unknown comp style — no matching hero profiles."
    comp = COMP_ARCHETYPES.get(tags[0])
    if not comp:
        return f"Comp style: {tags[0]}."
    return (
        f"Comp style: {comp['name']}. {comp['description']} "
        f"Beats: {', '.join(comp.get('beats', []))}. "
        f"Loses to: {', '.join(comp.get('loses_to', []))}."
    )


def get_heroes_for_prompt(hero_names: list[str]) -> str:
    """
    Return a concise prompt-ready block describing a set of heroes.
    Inject as a named section in build_draft_system_prompt when hero theory
    is relevant to the current question.
    """
    lines = []
    for name in hero_names:
        p = get_hero_profile(name)
        if p:
            synergies = ", ".join(p.get("synergies", [])[:3])
            teamup = p.get("teamup", "")
            abilities = ", ".join(p.get("abilities", [])[:5])
            counters = ", ".join(p.get("countered_by", [])[:2])
            lines.append(
                f"- {p.get('name', name)} ({p['role']}/{p['archetype']}): {p['function']}"
                + (f" Synergies: {synergies}." if synergies else "")
                + (f" Team-Up: {teamup}." if teamup else "")
                + (f" Key abilities: {abilities}." if abilities else "")
                + (f" Countered by: {counters}." if counters else "")
            )
    return "\n".join(lines) if lines else ""


# ---------------------------------------------------------------------------
# Hero attribute scores  (1–10 scale per dimension)
# ---------------------------------------------------------------------------
# Fields:
#   primary_style      : dominant comp style for this hero
#   secondary_style    : secondary style or None
#   mobility_score     : 1-10 — how independently mobile / hard to catch
#   sustain_score      : 1-10 — self-sustain or HP durability
#   poke_score         : 1-10 — threat at safe range
#   engage_score       : 1-10 — ability to initiate or create a fight
#   peel_score         : 1-10 — ability to protect allies from dives/flanks
#   execution_difficulty: 1-10 — mechanical/decision skill required to extract value
#   pressure_type      : engage | poke | hybrid | balanced
#   fight_pacing       : 1-10 — preferred pace, higher means faster fights
#   stability          : 1-10 — consistency under pressure (durability + peel)
#   execution_burden   : 1-10 — practical difficulty to execute cleanly
#   strategic_contribution: 1-10 — macro impact from pressure, initiation, and utility

HERO_SCORES: dict[str, dict] = {
    hero_name: hero_data.get("scores", {})
    for hero_name, hero_data in HERO_THEORY_DATA["heroes"].items()
}


def _clamp_score(value: float, low: int = 1, high: int = 10) -> int:
    return max(low, min(high, int(round(value))))


def _derive_pressure_type(scores: dict) -> str:
    engage = int(scores.get("engage_score", 0))
    poke = int(scores.get("poke_score", 0))
    if engage >= 7 and poke >= 7:
        return "hybrid"
    if engage - poke >= 3:
        return "engage"
    if poke - engage >= 3:
        return "poke"
    return "balanced"


def _derive_extended_score_fields(scores: dict) -> dict:
    mobility = int(scores.get("mobility_score", 0))
    sustain = int(scores.get("sustain_score", 0))
    poke = int(scores.get("poke_score", 0))
    engage = int(scores.get("engage_score", 0))
    peel = int(scores.get("peel_score", 0))
    execution = int(scores.get("execution_difficulty", 0))

    # Fight pacing skews toward engage and mobility, with poke as a smaller pacing factor.
    fight_pacing = _clamp_score((engage * 0.55) + (mobility * 0.35) + (poke * 0.10))
    stability = _clamp_score((sustain * 0.60) + (peel * 0.40))
    execution_burden = _clamp_score(execution)
    strategic_contribution = _clamp_score(
        (engage * 0.35) + (peel * 0.25) + (poke * 0.20) + (sustain * 0.20)
    )

    return {
        "pressure_type": _derive_pressure_type(scores),
        "fight_pacing": fight_pacing,
        "stability": stability,
        "execution_burden": execution_burden,
        "strategic_contribution": strategic_contribution,
    }


for _hero_name, _hero_scores in HERO_SCORES.items():
    _hero_scores.update(_derive_extended_score_fields(_hero_scores))


HERO_THEORY: dict[str, dict] = {
    hero_name: _combine_hero_theory_record(hero_name)
    for hero_name in sorted(set(HERO_GAME_DATA) | set(HERO_PROFILES) | set(HERO_SCORES))
}


def _validate_theory_data() -> None:
    profile_names = set(HERO_PROFILES)
    score_names = set(HERO_SCORES)
    game_names = set(HERO_GAME_DATA)

    errors: list[str] = []
    if missing_scores := sorted(profile_names - score_names):
        errors.append(f"Missing HERO_SCORES for: {', '.join(missing_scores)}")
    if missing_profiles := sorted(game_names - profile_names):
        errors.append(f"Missing HERO_PROFILES for: {', '.join(missing_profiles)}")

    for hero_name, profile in HERO_PROFILES.items():
        if profile.get("role") not in ROLES:
            errors.append(f"{hero_name}: invalid role {profile.get('role')!r}")
        if profile.get("archetype") not in ARCHETYPES:
            errors.append(f"{hero_name}: invalid archetype {profile.get('archetype')!r}")
        sub_role = profile.get("sub_role")
        if sub_role and sub_role not in SUBROLES:
            errors.append(f"{hero_name}: invalid sub_role {sub_role!r}")
        if profile.get("ban_priority") not in {"high", "medium", "low"}:
            errors.append(f"{hero_name}: invalid ban_priority {profile.get('ban_priority')!r}")
        for tag in profile.get("comp_tags", []):
            if tag not in COMP_ARCHETYPES:
                errors.append(f"{hero_name}: invalid comp tag {tag!r}")

    impact_scale = set(HERO_THEORY_DATA.get("impact_scale", {}))
    if impact_scale and impact_scale != ABILITY_IMPACT_TIERS:
        errors.append(f"impact_scale keys must be: {', '.join(sorted(ABILITY_IMPACT_TIERS))}")

    for hero_name, hero_data in HERO_THEORY_DATA["heroes"].items():
        for ability_key, ability in hero_data.get("kit", {}).items():
            if not isinstance(ability, dict):
                continue
            impact = ability.get("impact")
            if impact not in ABILITY_IMPACT_TIERS:
                errors.append(f"{hero_name}.{ability_key}: invalid impact {impact!r}")

    placeholder_names = {"Tankpool", "SupportPool"}
    for hero_name in sorted(profile_names - placeholder_names):
        game_data = get_hero_game_data(hero_name)
        if game_data and HERO_PROFILES[hero_name].get("role") != game_data.get("role"):
            errors.append(
                f"{hero_name}: profile role {HERO_PROFILES[hero_name].get('role')!r} "
                f"does not match game-data role {game_data.get('role')!r}"
            )

    if errors:
        raise ValueError("Invalid hero theory data:\n- " + "\n- ".join(errors))


_validate_theory_data()


# ---------------------------------------------------------------------------
# Playstyle comp assignments
# ---------------------------------------------------------------------------
# Describes which comp styles each hero fits into and WHY.
# Hybrid = works in both brawl AND dive (with explanation of why over others).
# triple_tank / triple_support = heroes valid for those specialty comp formats.

PLAYSTYLE_COMPS: dict[str, dict] = HERO_THEORY_DATA["playstyle_comps"]

# ---------------------------------------------------------------------------
# Score and playstyle helper functions
# ---------------------------------------------------------------------------

def get_hero_score(hero_name: str) -> dict | None:
    """Return the score dict for a hero, or None if not found."""
    resolved = _resolve_hero_name(hero_name, HERO_SCORES)
    if not resolved:
        return None
    return HERO_SCORES[resolved]


def get_heroes_by_playstyle(style: str) -> list[str] | dict:
    """
    Return heroes valid for a comp style.
    style: 'brawl' | 'dive' | 'poke' | 'hybrid' | 'triple_tank' | 'triple_support'
    Returns list of hero names, or for 'hybrid' returns the dict with reasons.
    """
    key = _style_key(style)
    comp = PLAYSTYLE_COMPS.get(key)
    if not comp:
        return []
    if key == "hybrid":
        return comp["heroes"]
    if key in ("triple_tank", "triple_support"):
        field = "valid_vanguards" if key == "triple_tank" else "valid_supports"
        return comp.get(field, [])
    return comp.get("heroes", [])


def describe_playstyle_comp(style: str) -> str:
    """Return a prompt-ready description of a comp style including example and why it works."""
    key = _style_key(style)
    comp = PLAYSTYLE_COMPS.get(key)
    if not comp:
        arch = COMP_ARCHETYPES.get(key)
        if arch:
            return f"{arch['name']}: {arch['description']} Beats: {', '.join(arch.get('beats', []))}. Loses to: {', '.join(arch.get('loses_to', []))}."
        return f"No data for comp style: {style}."
    lines = [comp["description"]]
    if "example_core" in comp:
        lines.append(f"Example core: {', '.join(comp['example_core'])}.")
    if "why_example_works" in comp:
        lines.append(comp["why_example_works"])
    if "requires" in comp:
        lines.append(f"Requirement: {comp['requires']}")
    return " ".join(lines)
