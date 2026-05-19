"""
llm.py — Stats-only agent for draft analysis (no theory, pure frequencies).

DESIGN:
  This module provides the agent loop interface without LLM reasoning.
  All responses are formatted from stats queries (de_draft_actions, de_team_hero_bias, etc.).
  No theory language ("too strong", "synergy", "comp archetype") — only data.

FUNCTIONS:
  - build_draft_system_prompt: Returns context (no theory injection)
  - run_agent_loop: Executes agent with stats tools only
  - stream_agent_loop: Streams agent responses (stub, returns None)
  - format_stats_response: Takes raw stats → formatted response with evidence

OUTPUT FORMAT:
  - Likely action (ban/pick) + evidence (counts, %s)
  - Impact (our win rate with/without)
  - Fallback options
  - Confidence level
  - NO theory language
"""

from __future__ import annotations

import re
from typing import Any, Callable


# Minimal tool definitions (no theory injection)
_AGENT_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_matchup_data",
            "description": (
                "Fetch statistical matchup data for two teams: ban frequencies, pick rates, "
                "hero pair statistics, map-specific distributions. "
                "Use for any question comparing two teams' historical draft patterns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "team_a_name": {"type": "string", "description": "Our team. Leave blank for currently selected team."},
                    "team_b_name": {"type": "string", "description": "Opponent team."},
                    "season": {"type": "string", "description": "Season filter (e.g., 'season 7') or 'all'."},
                    "map": {"type": "string", "description": "Map filter or 'all'."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_site_data",
            "description": (
                "Search team and player statistics from match history. "
                "Use for questions about hero pools, player stats, map records, scrim history."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search (e.g., 'Navi hero stats season 7', 'Fate player profile').",
                    },
                    "season": {"type": "string", "description": "Season filter or 'all'."},
                },
                "required": ["query"],
            },
        },
    },
]


def build_draft_system_prompt(
    context_text: str = "",
    site_context_text: str = "",
    personal_team: str = "",
    meta: dict | None = None,
    intent: str = "general",
) -> str:
    """Build a system prompt with stats context (no theory)."""
    
    parts = [
        "You are a draft analyst who reads match statistics only.",
        "Answer questions about draft patterns based on historical data (frequencies, ban/pick rates, hero pairs).",
        "Do NOT speculate about strategy or theory—cite actual numbers from the data.",
        "If data is missing, say so explicitly.",
        "",
    ]
    
    if site_context_text:
        parts.append("Available data:")
        parts.append(site_context_text)
        parts.append("")
    
    if context_text:
        parts.append("Additional context:")
        parts.append(context_text)
        parts.append("")
    
    if personal_team:
        parts.append(f"Focus on {personal_team}'s statistics where relevant.")
        parts.append("")
    
    meta = meta or {}
    if meta.get("live_draft_active"):
        parts.append("Live draft is active. Provide concise, data-backed observations.")
    else:
        parts.append("Provide detailed statistical analysis with actual counts or percentages.")
    
    return "\n".join(parts)


def format_stats_response(stats_data: dict) -> str:
    """
    Format raw stats into readable response (no theory language).
    
    Example input:
    {
        "likely_action": "ban",
        "hero": "Phoenix",
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
        "confidence": "medium-high",
    }
    
    Returns: Formatted text with evidence and numbers only (no theory).
    """
    lines = []
    
    # Header
    hero = stats_data.get("hero", "?")
    action = stats_data.get("likely_action", "").title()
    lines.append(f"Likely enemy {action}: {hero}")
    lines.append("")
    
    # Evidence section
    evidence = stats_data.get("evidence", [])
    if evidence:
        lines.append("Evidence:")
        for item in evidence:
            stat = item.get("stat", "")
            value = item.get("value", "")
            lines.append(f"- {stat}: {value}")
        lines.append("")
    
    # Impact section (our record)
    impact = stats_data.get("impact", {})
    if impact:
        available = impact.get("available", {})
        banned = impact.get("banned", {})
        
        if available:
            av_w, av_l = available.get("wins", 0), available.get("losses", 0)
            lines.append("Impact:")
            lines.append(f"- Our record with {hero} available: {av_w}–{av_l}")
        
        if banned:
            bn_w, bn_l = banned.get("wins", 0), banned.get("losses", 0)
            lines.append(f"- Our record with {hero} banned: {bn_w}–{bn_l}")
        lines.append("")
    
    # Fallback section
    fallback = stats_data.get("fallback", [])
    if fallback:
        lines.append("Best historical fallback:")
        for item in fallback:
            fb_hero = item.get("hero", "")
            played = item.get("played", 0)
            record = item.get("record", "?")
            lines.append(f"- {fb_hero} used in {played} maps")
            lines.append(f"- Fallback record: {record}")
        lines.append("")
    
    # Confidence
    confidence = stats_data.get("confidence", "")
    if confidence:
        lines.append(f"Confidence: {confidence}")
    
    return "\n".join(lines)


def run_agent_loop(
    user_message: str,
    system_prompt: str,
    tools: list[dict],
    tool_executor: Callable[[str, dict[str, Any]], str],
    temperature: float = 0.25,
    max_tokens: int | None = None,
    max_steps: int = 5,
    timeout: int = 30,
    deadline_seconds: float | None = None,
) -> str:
    """
    Execute agent loop (stats-only, no LLM reasoning).
    
    This is a simplified version that:
    1. Calls tools if available (tool_executor will handle them)
    2. Returns a placeholder response asking user to check tool results
    3. Does not call any external LLM
    
    Args:
      user_message: User's question
      system_prompt: System context (unused in stats-only mode)
      tools: Available tool definitions
      tool_executor: Function to call tools
      temperature, max_tokens, max_steps, timeout, deadline_seconds: Ignored (no LLM)
      
    Returns:
      A response combining tool results, or a fallback if no tools available
    """
    
    # If no tools, return a placeholder
    if not tools:
        return (
            "I can provide statistics from recorded matches. "
            "Use the search or matchup tools to fetch specific data, or ask a question about the current context."
        )
    
    # For now, we don't auto-invoke tools. The UI layer (flask routes) handles tool calling.
    # This function is a stub to satisfy the agent loop interface.
    return "Ready for analysis. Call tools to fetch data."


def stream_agent_loop(
    user_message: str,
    system_prompt: str,
    tools: list[dict],
    tool_executor: Callable[[str, dict[str, Any]], str],
    temperature: float = 0.25,
    max_tokens: int | None = None,
    max_steps: int = 5,
    timeout: int = 30,
) -> str | None:
    """Streaming version (stub). Returns None—streaming not implemented for stats-only mode."""
    return None


# Export tools and agent tools list
_AGENT_TOOLS = _AGENT_TOOLS  # Re-export for compatibility

__all__ = [
    "build_draft_system_prompt",
    "run_agent_loop",
    "stream_agent_loop",
    "format_stats_response",
    "_AGENT_TOOLS",
]
