"""Shared runtime for machine-agent tool execution and prefetch behavior."""

from __future__ import annotations

import re
from typing import Any, Callable


def normalize_season_filter(value: str | None) -> str | None:
    """Strip a leading season prefix and collapse all/all-like to None."""
    if not value or str(value).lower() == "all":
        return None
    return re.sub(r"^season\s*", "", str(value).strip(), flags=re.IGNORECASE) or None


def should_prefetch_site(intent: str, chat_context: dict, message: str) -> bool:
    """Return True when single-team site data should be loaded before the LLM runs."""
    q = (message or "").lower()
    has_explicit_matchup = bool(chat_context.get("team_b_id") or chat_context.get("team_b_name"))
    if has_explicit_matchup:
        return False
    if intent in {"comfort", "stats", "map"}:
        return True
    return any(
        phrase in q
        for phrase in (
            "hero pool",
            "comfort heroes",
            "team profile",
            "team overview",
            "player stats",
            "map record",
            "scrim history",
        )
    )


def prefetch_site_payload(
    message: str,
    season_value: str | None,
    *,
    site_answer_fn: Callable[[str, str | None], dict | None],
    site_context_text_fn: Callable[[dict], str],
) -> dict:
    """Load site context/answer ahead of tool-calling so answers are deterministic."""
    season = normalize_season_filter(season_value)
    try:
        site_result = site_answer_fn(message, season)
    except Exception as exc:
        import traceback as tb
        print(f"[machine] prefetch site error: {exc}\n{tb.format_exc()}")
        site_result = None
    if not site_result:
        return {"context": "", "answer": "", "meta": {}}

    site_meta = site_result.get("meta") or {}
    answer_text = (site_result.get("answer") or "").strip()
    raw_ctx = site_context_text_fn(site_meta.get("site_search") or {})
    merged_context = (answer_text + "\n\n" + raw_ctx).strip() if answer_text else raw_ctx
    return {
        "context": merged_context,
        "answer": answer_text,
        "meta": site_meta,
    }


def build_tool_executor(
    *,
    season_value: str,
    selected_map_name: str,
    chat_context: dict,
    personal_team_id: int | None,
    include_scrims: bool,
    include_tournaments: bool,
    tools_called: set[str],
    captured_meta: list[dict],
    captured_visuals: list[dict],
    captured_team_a_id: list,
    captured_team_b_id: list,
    resolve_team_by_name: Callable[[str], int | None],
    build_context_fn: Callable[[int, int, str, str], tuple[str, dict]],
    site_answer_fn: Callable[[str, str | None], dict | None],
    site_context_text_fn: Callable[[dict], str],
) -> Callable[[str, dict[str, Any]], str]:
    """Create a stateful tool executor closure for one chat request."""

    def execute_tool(fn_name: str, fn_args: dict[str, Any]) -> str:
        if fn_name == "get_matchup_data":
            team_a_name = (fn_args.get("team_a_name") or "").strip()
            team_b_name = (fn_args.get("team_b_name") or "").strip()
            season = fn_args.get("season") or season_value
            map_name = fn_args.get("map") or selected_map_name
            ta_id = (resolve_team_by_name(team_a_name) if team_a_name else None) \
                or chat_context.get("team_a_id") or personal_team_id
            tb_id = (resolve_team_by_name(team_b_name) if team_b_name else None) \
                or chat_context.get("team_b_id")

            if not ta_id or not tb_id:
                return (
                    "Cannot fetch matchup: no opponent team is selected. "
                    "Use search_site_data to answer single-team questions instead. "
                    "Only call get_matchup_data again if the user explicitly names an opponent."
                )

            captured_team_a_id[0] = ta_id
            captured_team_b_id[0] = tb_id
            context_text, meta = build_context_fn(ta_id, tb_id, season, map_name)
            captured_meta[0] = meta
            captured_visuals[0] = dict(meta.get("visuals") or {})
            return context_text

        if fn_name == "search_site_data":
            if "search_site_data" in tools_called:
                return (
                    "search_site_data was already called. "
                    "Do not call it again. Use the data already returned to answer the question."
                )
            tools_called.add("search_site_data")
            query = fn_args.get("query", "")
            season = normalize_season_filter(fn_args.get("season")) or normalize_season_filter(season_value)
            try:
                site_result = site_answer_fn(query, season)
            except Exception as exc:
                import traceback as tb
                print(f"[machine] search_site_data error: {exc}\n{tb.format_exc()}")
                site_result = None
            if not site_result:
                return (
                    "No data found in the database for that query. "
                    "Answer from your general knowledge about the team or player if possible. "
                    "Do not call any more tools."
                )
            site_meta = site_result.get("meta") or {}
            if site_meta.get("visuals"):
                captured_visuals[0].update(site_meta["visuals"])
            answer_text = site_result.get("answer", "")
            raw_ctx = site_context_text_fn(site_meta.get("site_search") or {})
            return (answer_text + "\n\n" + raw_ctx).strip() if answer_text else raw_ctx

        return f"Unknown tool: {fn_name}"

    return execute_tool
