"""
draft_rules.py — Scoring engine for the Marvel Rivals draft agent.

Turns raw match statistics into actionable scores.
The LLM receives these scores as context -- it does not compute them itself.

DESIGN INTENT:
  Every scoring function returns (score, reason) so the LLM can
  cite why a score was assigned. Keep functions pure: input data in,
  (score, explanation) out. No DB calls here -- callers pass in pre-fetched rows.

  Eventually this becomes the full "coach brain":
  - The DB provides raw counts.
  - draft_rules.py converts counts to ranked decisions with reasons.
  - llm.py reasons about the ranked decisions.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Draft decision framework
# ---------------------------------------------------------------------------
# Encodes coaching logic: how to convert stats into ban/protect/play decisions,
# how to select comps, and what makes heroes compatible.
# The LLM reads this as theory to apply to live data — not as instructions on
# how to format answers.

DRAFT_DECISION_FRAMEWORK: dict[str, object] = {

    "coach_theory_principles": {
        "description": "Coach-level theory for turning stats into play, protect, ban, and comp decisions.",
        "principles": [
            "Use stats to find anomalies first: anything that stands out strongly positive or negative is the starting point for the draft read.",
            "Try to create situations where the opponent is pushed into their negative anomalies and kept away from their positive anomalies.",
            "Choose the best-in-slot comp your team still has available for the map type being played.",
            "Perma bans come first when a hero is too strong to leave open as a one-sided advantage for the enemy.",
            "Certain bans force certain comps by removing lynchpin heroes. If the lynchpin disappears, the full comp can become too weak to play.",
            "Hero compatibility starts with who is strongest at the time, then checks whether those heroes share a real win condition on the current map.",
        ],
        "lynchpin_examples": [
            {
                "comp": "Hulk fastball",
                "lynchpins": ["Wolverine"],
                "logic": "If Wolverine is removed, Hulk fastball loses the hero that turns Hulk's setup into reliable kill pressure.",
            },
            {
                "comp": "Front-to-back",
                "lynchpins": ["Loki", "Groot"],
                "logic": "If Loki or Groot is removed, the comp loses key sustain, deception, walls, or space control that lets it hold the line.",
            },
        ],
    },

    "stat_based_decisions": {
        "description": (
            "How to use match stats to decide what to play, protect, and ban."
        ),
        "play_criteria": [
            "WR ≥ 55% on 10+ maps → strong keep candidate; treat as comfort anchor.",
            "High played count + positive record → proven, not just lucky.",
            "Pair WR ≥ 60% on 6+ games → the pairing is real; build around it.",
            "Low played count but high ban/protect rate → enemy respects it even if your data is thin; trust the signal.",
        ],
        "protect_criteria": [
            "Your team's top-WR heroes with 10+ maps are protect candidates.",
            "Any hero where the team's WR drops sharply without them in the lineup.",
            "Heroes that enable your preferred win condition (e.g., IW in triple support) — losing them forces a different comp style.",
            "If the opponent bans them frequently, that confirms your protect read.",
        ],
        "ban_criteria": [
            "Enemy hero with high comfort (played a lot, winning) → ban the comfort to force weakness.",
            "Enemy hero that specifically counters your best comp → ban the counter before it removes your game plan.",
            "Heroes with game-state-altering ults (Jeff, Star-Lord) → ban removes the coinflip regardless of comp.",
            "Attention rate (ban + protect + play) / total appearances → the higher this is, the more contested a hero is across both teams.",
        ],
        "pivot_check": (
            "If a hero has WR < 45% on 6+ maps, investigate who was in the lineup. "
            "Low WR on a comfort pick usually means the comp around it failed, not the hero itself. "
            "Check pair stats before benching a comfort pick entirely."
        ),
    },

    "comp_selection_theory": {
        "description": (
            "How to choose which comp style to run for a given match."
        ),
        "steps": [
            "1. Identify your team's highest-comfort roles and heroes. Build the comp around what your players execute best.",
            "2. Match comp style to map geometry. Open sightlines → poke. Chokepoints and close corridors → brawl. Elevated or spread backlines → dive.",
            "3. After bans resolve, re-evaluate which of your preferred comp styles is still intact. If a key hero was banned, pivot to the next-best option.",
            "4. Counter-draft only when you have equal comfort on the counter comp. Picking a counter you can't execute loses the draft advantage.",
            "5. Consistency beats optimization. If your team runs dive at 60% WR and brawl at 45%, run dive every time unless it is directly counter-drafted.",
        ],
        "comp_alignment_rule": (
            "Every hero in the comp should serve the same win condition. "
            "A brawl tank + dive supports + poke duelist is not a comp — it is three individual heroes with no shared goal. "
            "Ask: do all six heroes benefit from the same fight shape?"
        ),
    },

    "perma_ban_logic": {
        "description": "Why certain heroes are always banned first, regardless of matchup.",
        "reasons": [
            "Versatility tax: a hero that enables too many comp styles has no single counter. Banning it removes the option space rather than countering one specific threat. (e.g., Invisible Woman enables brawl, dive, poke, and triple support — no single comp answer.)",
            "Game-state ult: a hero whose ultimate changes who wins the map regardless of the fight leading up to it. Removing it removes the coinflip. (e.g., Jeff's ult can eject all six enemies off an objective in seconds.)",
            "Comfort dependency: if the opponent runs a hero in 80% of maps and wins 65% of those, that hero is their win condition. Removing it forces them to play worse.",
            "Counter-removal: if a hero directly negates your comp's win condition (e.g., Invisible Woman bubble stops your dive pick), banning it first protects your game plan before you commit to the comp.",
        ],
        "priority_order": (
            "First bans: remove the enemy's highest-comfort win condition OR their best counter to your game plan. "
            "Middle bans: remove versatility heroes that enable multiple comp options. "
            "Last bans: force the specific comp shape you want to play into by removing flex options."
        ),
    },

    "ban_forcing_comp": {
        "description": "How bans create comp constraints — for both your team and the enemy.",
        "examples": [
            {
                "ban": "Venom",
                "effect": "Removes the primary dive tank. Enemy has no fast frontline diver. Forces them toward brawl or poke comp. Your team can now protect your backline without planning for Venom-speed dives.",
            },
            {
                "ban": "Invisible Woman",
                "effect": "Removes peel and bubble protection. Every diving hero your team runs now has an easier path. Enemy supports have no displacement safety net. Makes dive significantly more reliable.",
            },
            {
                "ban": "Jeff the Land Shark",
                "effect": "Removes the displacement support. Forces the enemy into a support that can't reposition five players at once. Reduces ult-moment coinflips. Makes aggressive play safer.",
            },
            {
                "ban": "Mantis",
                "effect": "Removes damage amplification and sleep dart control from all enemy comps. Their damage ceiling drops. Their support can no longer create free picks via sleep. Makes your team's cooldowns safer to use.",
            },
            {
                "ban": "Luna Snow",
                "effect": "Removes the highest-throughput healer. Forces the enemy into a lower-HPS support. Makes attrition brawls harder for them. Increases the value of burst damage.",
            },
        ],
        "logic": (
            "A ban is not just removing a hero — it is removing a game plan. "
            "Always ask: if I ban this, what comp can they still run? If the answer is 'still multiple strong options', consider banning a different hero that collapses more of their draft space."
        ),
    },

    "hero_compatibility": {
        "description": "What makes heroes in a comp compatible with each other.",
        "principles": [
            "Current strength first: the best comp starts from the strongest available heroes in the current patch, map, and draft state.",
            "Range diversity: good comps have threats at multiple ranges. All six heroes at the same range compete for the same space and lose to one counter.",
            "Win condition alignment: every hero in the comp should enable the same win condition. Dive tank + brawl duelist + poke support = three unrelated heroes, not a comp.",
            "Sustain timing match: supports must heal at the tempo the comp creates. Dive needs instant burst recovery (Cloak & Dagger, Luna Snow). Brawl needs sustained throughput (Rocket, Luna Snow). Mixing a dive comp with a slow-sustain support creates a gap between when fights happen and when healing arrives.",
            "Synergy activation: pair heroes that unlock each other's kits. A Mantis sleep sets up a Phoenix ult chain. A Strange portal repositions for a Captain America engage. Check synergy tags before finalizing.",
            "Peel coverage: if the comp has a carry that enemies will dive, the comp needs at least one hero who can peel (displacement, interrupt, or mobility) to protect that carry. A comp with no peel loses to any dive.",
            "Avoid overlap on the same weakness: two dive duelists who both lose to the same counter (e.g., Psylocke + Daredevil both losing to strong peel support) means one ban or one counter removes 33% of your damage. Diversify so counters don't remove the entire win condition at once.",
        ],
    },
}


def get_draft_decision_theory_prompt() -> str:
    """
    Return the compact coach-theory block used by the machine prompt.
    Keep this short so live draft calls stay fast and focused.
    """
    coach = DRAFT_DECISION_FRAMEWORK.get("coach_theory_principles", {})
    principles = coach.get("principles", [])
    examples = coach.get("lynchpin_examples", [])

    lines = [
        "Use this theory when choosing what to play, protect, and ban:",
    ]
    for item in principles:
        lines.append(f"- {item}")
    if examples:
        lines.append("Lynchpin ban examples:")
        for example in examples:
            comp = example.get("comp", "Unknown comp")
            lynchpins = ", ".join(example.get("lynchpins", []))
            logic = example.get("logic", "")
            lines.append(f"- {comp}: remove {lynchpins}. {logic}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

def score_confidence(n_samples: int, winrate: float | None = None) -> tuple[str, str]:
    """
    Grade confidence based on sample size and winrate spread.

    Returns (label, reason).
    label: "High" | "Medium" | "Low" | "Very Low"
    """
    if n_samples >= 20:
        label, reason = "High", f"{n_samples} maps -- large sample, strong signal."
    elif n_samples >= 8:
        label, reason = "Medium", f"{n_samples} maps -- moderate sample, directionally reliable."
    elif n_samples >= 3:
        label, reason = "Low", f"{n_samples} maps -- small sample, treat as early signal only."
    else:
        label, reason = "Very Low", f"{n_samples} maps -- insufficient data, exercise caution."

    if winrate is not None and n_samples >= 3:
        spread = abs(winrate - 0.5)
        if spread < 0.05 and n_samples < 10:
            label = "Low"
            reason += " Winrate too close to 50% to distinguish signal from noise."

    return label, reason


# ---------------------------------------------------------------------------
# Leverage scoring
# ---------------------------------------------------------------------------

def score_leverage(hero_name: str, hero_data: dict) -> tuple[float, str]:
    """
    Score a hero's draft leverage on a 0-100 scale.

    Leverage measures how much the draft outcome depends on this hero,
    combining draft attention (ban+protect frequency) with winrate signal.

    hero_data fields used: ban_count, protect_count, played_count,
                           played_wins, played_losses.
    Returns (score 0-100, reason).
    """
    ban     = hero_data.get("ban_count", 0) or 0
    protect = hero_data.get("protect_count", 0) or 0
    played  = hero_data.get("played_count", 0) or 0
    wins    = hero_data.get("played_wins", 0) or 0
    losses  = hero_data.get("played_losses", 0) or 0
    total   = played + ban + protect

    if total == 0:
        return 0.0, f"{hero_name}: no draft appearances."

    # How often does this hero come up in the draft at all?
    attention_rate = min(1.0, (ban + protect) / max(1, total))

    # Winrate signal (+/- from 50%)
    wr = wins / (wins + losses) if (wins + losses) > 0 else 0.5
    wr_boost = (wr - 0.5) * 2  # -1 to +1

    # Ban weight: heroes banned more often have proven strategic importance
    ban_weight = ban / max(1, total)

    score = round(
        (attention_rate * 60) + (ban_weight * 20) + (wr_boost * 20),
        1
    )
    score = max(0.0, min(100.0, score))

    reason = (
        f"{hero_name}: {ban} bans, {protect} protects, {played} played "
        f"({wins}-{losses}). Leverage: {score}/100."
    )
    return score, reason


# ---------------------------------------------------------------------------
# Ban priority scoring
# ---------------------------------------------------------------------------

def score_ban_priority(
    hero_name: str,
    hero_data: dict,
    is_opponent: bool = True,
    contested: bool = False,
) -> tuple[float, str]:
    """
    Score the priority of banning a specific hero.

    is_opponent: True  = offensive ban (deny their comfort)
                 False = neutral ban (remove contested hero)
    contested:   True  = both teams have a strong claim

    Returns (priority 0-100, reason).
    """
    lev, lev_reason = score_leverage(hero_name, hero_data)

    contested_bonus = 15.0 if contested else 0.0

    played = hero_data.get("played_count", 0) or 0
    ban    = hero_data.get("ban_count", 0) or 0

    # Comfort signal: if they play it more than they get banned off it, it's a comfort pick
    comfort_signal = played / max(1, played + ban)
    comfort_weight = comfort_signal * 20 if is_opponent else 0.0

    priority = round(lev + comfort_weight + contested_bonus, 1)
    priority = max(0.0, min(100.0, priority))

    reason = (
        f"Ban priority for {hero_name}: {priority}/100. "
        f"{lev_reason}"
        + (f" Contested bonus: +{contested_bonus:.0f}." if contested else "")
        + (f" Comfort signal: {comfort_signal:.0%}." if is_opponent else "")
    )
    return priority, reason


def prioritize_bans(
    hero_data_list: list[dict],
    n: int = 4,
    contested_names: set[str] | None = None,
) -> list[dict]:
    """
    Rank ban candidates from a list of opponent hero bias rows.

    Each row: {hero, ban_count, protect_count, played_count, played_wins, played_losses}.
    Returns top-n as [{hero, score, reason}] sorted descending.
    """
    contested = set(contested_names or [])
    results = []
    for row in hero_data_list:
        hero_name = row.get("hero", "")
        if not hero_name:
            continue
        score, reason = score_ban_priority(
            hero_name,
            row,
            is_opponent=True,
            contested=(hero_name in contested),
        )
        results.append({"hero": hero_name, "score": score, "reason": reason})
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:n]


# ---------------------------------------------------------------------------
# Pivot risk scoring
# ---------------------------------------------------------------------------

def score_pivot_risk(
    primary_hero: str,
    pivot_heroes: list[dict],
    ban_count: int = 0,
) -> tuple[str, str]:
    """
    Score how risky it is to ban a player's primary hero (forcing a pivot).

    pivot_heroes: [{hero, appearances, wins, losses}] sorted by appearances desc.
    ban_count:    historical ban count for this hero (pressure signal).

    Returns (risk_label, reason).
    risk_label: "Safe" | "Moderate" | "High" | "Critical"
    """
    if not pivot_heroes:
        return (
            "Critical",
            f"No recorded pivots from {primary_hero}. Ban forces an unknown/unpractised hero.",
        )

    top = pivot_heroes[0]
    apps   = top.get("appearances", 0) or 0
    wins   = top.get("wins", 0) or 0
    losses = top.get("losses", 0) or 0
    wr     = wins / (wins + losses) if (wins + losses) > 0 else 0.0

    if apps >= 15 and wr >= 0.5:
        label = "Safe"
        reason = (
            f"Banning {primary_hero} risks little -- "
            f"pivot to {top['hero']} is well-practised ({apps} apps, {wr:.0%} WR)."
        )
    elif apps >= 8:
        label = "Moderate"
        reason = (
            f"Pivot to {top['hero']} is moderate ({apps} apps, {wr:.0%} WR). "
            f"Worth the ban if {primary_hero} has high leverage."
        )
    elif apps >= 3:
        label = "High"
        reason = (
            f"Pivot to {top['hero']} is thin ({apps} apps, {wr:.0%} WR). "
            f"Banning {primary_hero} likely forces a real comfort drop."
        )
    else:
        label = "Critical"
        reason = (
            f"Almost no recorded pivot from {primary_hero} "
            f"(best: {top.get('hero','?')} with {apps} apps). "
            f"Ban forces an unpractised hero."
        )

    if ban_count >= 5:
        reason += f" Already banned {ban_count}x historically -- opponent knows this threat."

    return label, reason


# ---------------------------------------------------------------------------
# Comp matchup scoring
# ---------------------------------------------------------------------------

# Advantage matrix: comp_a_tag vs comp_b_tag → advantage for comp_a
# +1.0 = comp_a clear advantage | -1.0 = comp_b clear advantage | 0.0 = neutral
_COMP_ADVANTAGE: dict[str, dict[str, float]] = {
    "dive":  {"poke": 1.0,  "brawl": -0.5, "dive": 0.0, "rush":  0.5, "flex": 0.0},
    "poke":  {"dive": -1.0, "brawl": 0.5,  "poke": 0.0, "rush":  0.0, "flex": 0.0},
    "brawl": {"dive": 0.5,  "poke": -0.5,  "brawl": 0.0,"rush":  0.5, "flex": 0.0},
    "rush":  {"dive": -0.5, "poke": 0.0,   "brawl": -0.5,"rush": 0.0, "flex": 0.0},
    "flex":  {"dive": 0.0,  "poke": 0.0,   "brawl": 0.0, "rush": 0.0, "flex": 0.0},
}


def score_comp_matchup(
    comp_a_tags: list[str],
    comp_b_tags: list[str],
) -> tuple[float, str]:
    """
    Score the matchup between two comps by primary archetype tag.

    Returns (advantage_score, reason):
      > 0 → comp_a favored
      < 0 → comp_b favored
      = 0 → neutral
    """
    score = 0.0
    comparisons = []
    a_tag = comp_a_tags[0] if comp_a_tags else ""
    b_tag = comp_b_tags[0] if comp_b_tags else ""
    if a_tag and b_tag:
        adv = _COMP_ADVANTAGE.get(a_tag, {}).get(b_tag, 0.0)
        score = adv
        if adv > 0:
            comparisons.append(f"{a_tag} beats {b_tag} (+{adv:.1f})")
        elif adv < 0:
            comparisons.append(f"{b_tag} beats {a_tag} ({adv:.1f})")
        else:
            comparisons.append(f"{a_tag} vs {b_tag} is neutral")

    reason = comparisons[0] if comparisons else "No comp tags to compare."
    return round(score, 2), reason


# ---------------------------------------------------------------------------
# Draft risk scoring
# ---------------------------------------------------------------------------

def score_draft_risk(
    comfort_heroes_open: list[str],
    pivot_risks: list[tuple[str, str]],
    contested_unresolved: list[str],
) -> tuple[str, str]:
    """
    Assess overall draft risk at a given point in the draft.

    comfort_heroes_open:   opponent comfort heroes not yet banned or taken.
    pivot_risks:           [(hero, risk_label)] from score_pivot_risk.
    contested_unresolved:  contested heroes neither team has addressed.

    Returns (risk_level, summary).
    risk_level: "High" | "Medium" | "Low"
    """
    risk_score = 0
    reasons: list[str] = []

    high_pivots = [h for h, r in pivot_risks if r in ("High", "Critical")]
    if high_pivots:
        risk_score += len(high_pivots) * 2
        reasons.append(f"High pivot risk on: {', '.join(high_pivots)}")

    if contested_unresolved:
        risk_score += len(contested_unresolved)
        reasons.append(f"Unresolved contested heroes: {', '.join(contested_unresolved)}")

    if comfort_heroes_open:
        risk_score += len(comfort_heroes_open)
        reasons.append(f"Opponent comforts still open: {', '.join(comfort_heroes_open)}")

    if risk_score >= 6:
        level = "High"
    elif risk_score >= 3:
        level = "Medium"
    else:
        level = "Low"

    summary = f"Draft risk: {level} (score {risk_score}). " + " | ".join(reasons)
    return level, summary


# ---------------------------------------------------------------------------
# Convenience: build a scored ban list from matchup meta
# ---------------------------------------------------------------------------

def ranked_bans_from_meta(meta: dict, n: int = 4) -> list[dict]:
    """
    Given the matchup meta dict produced by _machine_chat_build_context,
    extract opponent hero bias rows and return a ranked ban list.

    meta keys used: opponent_hero_bias, contested_heroes
    """
    bias_rows  = meta.get("opponent_hero_bias") or []
    contested  = {row.get("hero", "") for row in (meta.get("contested_heroes") or [])}
    return prioritize_bans(bias_rows, n=n, contested_names=contested)
