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
from typing import Any, Callable, Iterator


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
    """Build a practical Marvel Rivals draft-reasoner prompt."""
    
    parts = [
        "You are a practical Marvel Rivals draft assistant for coaches.",
        "Keep answers simple and directly useful.",
        "",
        "Most important ban rule:",
        "Predict the opponent's likely ban from their recorded ban history first.",
        "Do not claim they will likely ban a hero they have never banned when they have meaningful ban history.",
        "If a hero is dangerous for us but not in their ban history, call it a concern, not the likely ban.",
        "",
        "Comp rules:",
        "Only name comps that appear in verified imported scrim history or verified external scouting.",
        "If no map-specific comp exists, say that clearly.",
        "Do not invent ideal comps, meta comps, or fallback comps.",
        "The exact ban scenario does not need to match one historical match, but named comps must still be verified.",
        "",
        "Draft reasoning rules:",
        "Do not choose our ban only because enemy comp dependency is high.",
        "Compare enemy value removed, our cost, coach hero-pool ratings, and whether the hero/comp is better for us than for them.",
        "If a ban hurts our stronger hero pool or verified Team Canada comp, avoid it or state why the trade-off is still worth it.",
        "Use confidence labels based on evidence strength and do not overstate weak evidence.",
        "Use coach override notes only when non-empty.",
        "Treat external scouting/meta as separate from Team Canada scrim evidence.",
        "Team Canada scrim comps are higher priority for what we should play; external scouting suggests trend and tendency.",
        "Use legal ban rules, opponent ban profile, scenario branches, hero stats, verified comps, external context, coach ratings, and why-not evidence when available.",
        "",
        "Output format (exact):",
        "we are banning: <first or second>",
        "confidence: <strong, medium, or weak with a short reason>",
        "they will ban: <hero or unclear>",
        "we should ban: <hero>",
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


def _tool_names(tools: list[dict]) -> set[str]:
    return {
        (tool.get("function") or {}).get("name", "")
        for tool in (tools or [])
        if (tool.get("function") or {}).get("name")
    }


def _system_prompt_has_opponent(system_prompt: str) -> bool:
    match = re.search(r"opponent:\s*([^;\n]+)", system_prompt or "", flags=re.IGNORECASE)
    if not match:
        return False
    opponent = (match.group(1) or "").strip().lower()
    return opponent not in {"", "not set", "none", "null"}


def _prefer_site_search(user_message: str) -> bool:
    message = (user_message or "").lower()
    site_phrases = (
        "hero pool",
        "comfort heroes",
        "team profile",
        "team overview",
        "player stats",
        "player profile",
        "map record",
        "map records",
        "scrim history",
    )
    return any(phrase in message for phrase in site_phrases)


def _prefer_matchup_search(user_message: str) -> bool:
    message = (user_message or "").lower()
    matchup_terms = (
        " vs ",
        "versus",
        "against",
        "opponent",
        "enemy",
        "matchup",
        "draft",
        "ban",
        "protect",
        "comp",
        "counter",
        "pivot",
        "slot",
    )
    return any(term in message for term in matchup_terms)


def _plan_tool_calls(user_message: str, system_prompt: str, tools: list[dict]) -> list[tuple[str, dict[str, Any]]]:
    names = _tool_names(tools)
    has_search = "search_site_data" in names
    has_matchup = "get_matchup_data" in names
    has_opponent = _system_prompt_has_opponent(system_prompt)

    if not names:
        return []

    if has_search and (not has_opponent or _prefer_site_search(user_message)):
        plan = [("search_site_data", {"query": user_message})]
        if has_matchup and has_opponent and _prefer_matchup_search(user_message):
            return [("get_matchup_data", {})] + plan
        return plan

    if has_matchup and (has_opponent or _prefer_matchup_search(user_message)):
        return [("get_matchup_data", {})]

    if has_search:
        return [("search_site_data", {"query": user_message})]
    if has_matchup:
        return [("get_matchup_data", {})]
    return []


def _execute_tool_plan(
    user_message: str,
    system_prompt: str,
    tools: list[dict],
    tool_executor: Callable[[str, dict[str, Any]], str],
) -> tuple[str, list[dict[str, Any]]]:
    plan = _plan_tool_calls(user_message, system_prompt, tools)
    if not plan:
        return "", []

    events: list[dict[str, Any]] = []
    fallback_prefixes = (
        "cannot fetch matchup:",
        "no data found in the database",
        "unknown tool:",
    )
    last_text = ""

    for fn_name, fn_args in plan:
        events.append({"type": "tool_start", "tool": fn_name, "args": fn_args})
        result = (tool_executor(fn_name, fn_args) or "").strip()
        events.append({"type": "tool_end", "tool": fn_name, "args": fn_args, "result": result})
        if fn_name == "get_matchup_data":
            if result:
                last_text = result
            continue
        if result and not result.lower().startswith(fallback_prefixes):
            return result, events
        if result:
            last_text = result

    if plan and all(fn_name == "get_matchup_data" for fn_name, _fn_args in plan):
        return "", events
    return last_text, events


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
    
    answer, _events = _execute_tool_plan(user_message, system_prompt, tools, tool_executor)
    return answer


def stream_agent_loop(
    user_message: str,
    system_prompt: str,
    tools: list[dict],
    tool_executor: Callable[[str, dict[str, Any]], str],
    temperature: float = 0.25,
    max_tokens: int | None = None,
    max_steps: int = 5,
    timeout: int = 30,
    deadline_seconds: float | None = None,
) -> Iterator[dict[str, Any]]:
    """Streaming-compatible wrapper for the stats-only loop.

    The machine chat route expects an event iterator. This stub keeps the same
    non-LLM behavior as ``run_agent_loop`` while providing the minimal event
    sequence the caller consumes.
    """
    answer, events = _execute_tool_plan(user_message, system_prompt, tools, tool_executor)
    for event in events:
        yield event
    yield {"type": "text", "text": answer or ""}
    yield {"type": "done", "text": answer or ""}


# Export tools and agent tools list
_AGENT_TOOLS = _AGENT_TOOLS  # Re-export for compatibility

__all__ = [
    "build_draft_system_prompt",
    "run_agent_loop",
    "stream_agent_loop",
    "format_stats_response",
    "_AGENT_TOOLS",
]
