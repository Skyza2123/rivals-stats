"""
Example usage: Sequential predictor + stats formatter.

This module shows the complete flow:
1. Query DB for raw stats (ban frequencies, win rates)
2. Compute P(next_hero) using sequential_predictor
3. Format as readable response using llm.format_stats_response

No theory language anywhere — only data citations.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict
from draft_engine.sequential_predictor import build_sequential_predictor
from draft_engine.llm import format_stats_response


def predict_enemy_ban(
    db_path: str,
    team_name: str,
    enemy_team_name: str,
    current_bans: list[str],
    current_picks: list[str],
    all_heroes: list[str],
    pick_order: int = 1,
    season: Optional[str] = None,
) -> str:
    """
    Predict likely enemy ban and format with evidence.
    
    Returns: Formatted response like:
      Likely enemy ban: Phoenix
      Evidence:
      - Banned vs us: 6/11 maps
      - Banned on Convoy: 4/5 maps
      Impact:
      - Our record with Phoenix available: 5–2
      - Our record with Phoenix banned: 1–3
      Best historical fallback:
      - Luna Snow used in 3 maps
      - Fallback record: 2–1
      Confidence: medium-high
    """
    
    # 1. Get probability distribution for next ban
    predictor = build_sequential_predictor(db_path, team_name=enemy_team_name)
    probabilities = predictor.predict_next_pick(
        current_bans=current_bans,
        current_picks=current_picks,
        team_slot="team1",  # Enemy's turn
        pick_order=pick_order,
        available_heroes=all_heroes,
        season=season,
    )
    
    if not probabilities:
        return "Insufficient data to predict ban."
    
    # 2. Find top candidate
    top_hero = max(probabilities, key=probabilities.get)
    top_prob = probabilities[top_hero]
    
    # 3. Query evidence (ban frequencies, impact on our record)
    # This would connect to your analytics module to fetch:
    # - How often this hero was banned vs your team
    # - Your team's record with/without the hero available
    # - Fallback heroes when this one is banned
    
    evidence_data = _gather_ban_evidence(
        db_path,
        team_name,
        enemy_team_name,
        top_hero,
        season,
    )
    
    # 4. Format for output
    stats_input = {
        "likely_action": "ban",
        "hero": top_hero,
        "evidence": evidence_data.get("evidence", []),
        "impact": evidence_data.get("impact", {}),
        "fallback": evidence_data.get("fallback", []),
        "confidence": _confidence_level(top_prob),
    }
    
    return format_stats_response(stats_input)


def _gather_ban_evidence(
    db_path: str,
    our_team: str,
    enemy_team: str,
    hero: str,
    season: Optional[str] = None,
) -> dict:
    """
    Query ban statistics for a hero against a specific team.
    
    Returns evidence, impact, and fallback data.
    (This is a stub; implement queries against your de_draft_actions
    and matchup analysis tables.)
    """
    return {
        "evidence": [
            {"stat": "Banned vs us", "value": "6/11 maps"},
            {"stat": "Banned on Convoy", "value": "4/5 maps"},
            {"stat": "Banned after Black Cat", "value": "3/4 maps"},
        ],
        "impact": {
            "available": {"wins": 5, "losses": 2},
            "banned": {"wins": 1, "losses": 3},
        },
        "fallback": [
            {"hero": "Luna Snow", "played": 3, "record": "2-1"},
        ],
    }


def _confidence_level(probability: float) -> str:
    """Convert probability to confidence label."""
    if probability >= 0.4:
        return "high"
    elif probability >= 0.25:
        return "medium-high"
    elif probability >= 0.15:
        return "medium"
    else:
        return "low"


# Example usage
if __name__ == "__main__":
    # This would be called from routes/machine.py or a template
    response = predict_enemy_ban(
        db_path="./rivals_stats_dump.sql",  # Your SQLite DB
        team_name="Our Team",
        enemy_team_name="Enemy Team",
        current_bans=[],
        current_picks=["Black Cat"],
        all_heroes=["Phoenix", "Luna Snow", "Hela", "Hulk", "Magneto"],
        pick_order=1,
        season="season 7",
    )
    print(response)
