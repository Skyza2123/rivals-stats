"""Shared machine-agent tool menu and schemas."""

from __future__ import annotations

TOOL_LABELS: dict[str, str] = {
    "get_matchup_data": "Fetching matchup data",
    "search_site_data": "Searching site data",
}

AGENT_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_matchup_data",
            "description": (
                "Fetch draft matchup data for two teams: ban priorities, protect targets, "
                "comfort heroes, contested heroes, volatile heroes, force paths, comp paths, "
                "map consensus, and enemy comp options. "
                "Use whenever the question involves bans, protects, comps, pivots, "
                "risk, confidence, or map strategy between two specific teams."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "team_a_name": {
                        "type": "string",
                        "description": "Name of our team (team A). Leave blank to use the currently selected team.",
                    },
                    "team_b_name": {
                        "type": "string",
                        "description": "Name of the opponent team (team B).",
                    },
                    "season": {
                        "type": "string",
                        "description": "Season filter such as 'season 7', or 'all' for all seasons.",
                    },
                    "map": {
                        "type": "string",
                        "description": "Map name filter, or 'all' for all maps.",
                    },
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
                "Search team, player, hero, and map statistics from recorded match history. "
                "Use for questions about a team's hero pool, player hero stats and win rates, "
                "map records, hero ban/protect counts, scrim history, and general team profiles. "
                "Works for any team or player by name and does not require an opponent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Natural-language search query. Examples: "
                            "'Navi hero stats season 7', 'Fate player profile', "
                            "'Strange ban rate', 'Virtus Pro map records'."
                        ),
                    },
                    "season": {
                        "type": "string",
                        "description": "Season filter, e.g. 'season 7', or 'all'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


def build_machine_tool_hint(
    our_team: str,
    opponent: str,
    season_value: str,
    selected_map_name: str,
) -> str:
    """System prompt hint that mirrors the explicit tool-routing style used in Parsertime."""
    return (
        f"Current UI context - our team: {our_team or 'not set'}; "
        f"opponent: {opponent}; season: {season_value}; map filter: {selected_map_name}.\n"
        "Tool selection menu:\n"
        "1) search_site_data: single-team/site questions (hero pool, comfort heroes, player stats, team profile, map records).\n"
        "2) get_matchup_data: two-team matchup questions (bans, force paths, comp matchups, draft analysis vs opponent).\n"
        "Call each tool at most once per question and do not retry the same tool."
    )
