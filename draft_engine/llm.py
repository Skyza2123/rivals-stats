"""
llm.py — OpenAI LLM integration for the draft agent.

Sends chat completion requests to the OpenAI API and returns the assistant's reply text.

Configuration (via environment / .env):
    OPENAI_API_KEY    — required; your OpenAI API key
    OPENAI_DRAFT_MODEL— optional; defaults to gpt-4o-mini
    OPENAI_MAX_TOKENS — optional; defaults to 768
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

_OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
_DEFAULT_MODEL = "gpt-4o-mini"
_DEFAULT_MAX_TOKENS = 768
_TIMEOUT_SECONDS = 30


def _get_api_key() -> str | None:
    # OPENAI_API_KEY is the canonical production setting.  MINIMAX_API_KEY is
    # accepted as a legacy alias because older sample env files used that name.
    return (
        os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("MINIMAX_API_KEY", "").strip()
        or None
    )


def _get_model() -> str:
    return (
        os.environ.get("OPENAI_DRAFT_MODEL", "").strip()
        or os.environ.get("MINIMAX_MODEL", "").strip()
        or _DEFAULT_MODEL
    )


def _get_max_tokens() -> int:
    try:
        return int(
            os.environ.get("OPENAI_MAX_TOKENS", "").strip()
            or os.environ.get("MINIMAX_MAX_TOKENS", "").strip()
        ) or _DEFAULT_MAX_TOKENS
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TOKENS


def _strip_think_tags(text: str) -> str:
    """Remove <think>…</think> reasoning tokens emitted by MiniMax reasoning models."""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def call_minimax_chat(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    timeout: int = _TIMEOUT_SECONDS,
) -> str | None:
    """
    POST `messages` to the Minimax chat completion endpoint.

    Returns the assistant reply text (think-tags stripped), or None on any error
    (missing API key, network failure, unexpected response shape, etc.).
    """
    api_key = _get_api_key()
    if not api_key:
        return None

    payload = {
        "model": model or _get_model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or _get_max_tokens(),
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _OPENAI_API_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        choices = body.get("choices") or []
        if not choices:
            return None
        content = (choices[0].get("message") or {}).get("content") or ""
        return _strip_think_tags(content) or None
    except urllib.error.HTTPError as exc:
        # Log status code so the caller can diagnose auth/quota issues
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            error_body = ""
        print(f"[llm] OpenAI HTTP {exc.code}: {error_body[:300]}")
        return None
    except Exception as exc:
        print(f"[llm] OpenAI call failed: {exc}")
        return None


def run_agent_loop(
    user_message: str,
    system_prompt: str,
    tools: list[dict],
    tool_executor,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    max_steps: int = 5,
    timeout: int = _TIMEOUT_SECONDS,
) -> str | None:
    """
    Agentic tool-calling loop (mirrors Parsertime's stopWhen: stepCountIs(5) pattern).

    Sends the message to OpenAI with tool definitions. If the model calls a tool,
    executes it via tool_executor(name, args) -> str, then sends the result back.
    Repeats up to max_steps. Returns the final assistant text, or None on failure.
    """
    api_key = _get_api_key()
    if not api_key:
        return None

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    for _step in range(max_steps):
        payload: dict = {
            "model": model or _get_model(),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or _get_max_tokens(),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _OPENAI_API_URL,
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = ""
            print(f"[llm] OpenAI HTTP {exc.code}: {error_body[:300]}")
            return None
        except Exception as exc:
            print(f"[llm] OpenAI call failed: {exc}")
            return None

        choices = body.get("choices") or []
        if not choices:
            return None

        choice = choices[0]
        finish_reason = choice.get("finish_reason", "")
        asst_msg = choice.get("message") or {}
        tool_calls = asst_msg.get("tool_calls") or []

        # Final answer — model is done
        if finish_reason == "stop" or not tool_calls:
            content = _strip_think_tags(asst_msg.get("content") or "")
            return content or None

        # Model wants to call tools — execute and loop
        messages.append(asst_msg)
        for call in tool_calls:
            call_id = call.get("id", "")
            fn = call.get("function") or {}
            fn_name = fn.get("name", "")
            try:
                fn_args = json.loads(fn.get("arguments", "{}") or "{}")
            except json.JSONDecodeError:
                fn_args = {}
            try:
                result = tool_executor(fn_name, fn_args)
            except Exception as exc:
                result = f"Tool error: {exc}"
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": str(result),
            })

    return None


def stream_agent_loop(
    user_message: str,
    system_prompt: str,
    tools: list[dict],
    tool_executor,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    max_steps: int = 5,
    timeout: int = _TIMEOUT_SECONDS,
):
    """
    Agentic tool-calling loop that yields SSE event dicts (mirrors Parsertime's stepCountIs(5)).

    Yields dicts of these types:
      {"type": "tool_start", "name": str, "label": str}   — before calling a tool
      {"type": "tool_end",   "name": str, "label": str}   — after tool returns
      {"type": "text",       "text": str}                  — the final answer
      {"type": "done"}                                      — stream finished
      {"type": "error",      "text": str}                  — on failure
    """
    api_key = _get_api_key()
    if not api_key:
        yield {"type": "error", "text": "No API key configured."}
        return

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    _tool_labels = {
        "get_matchup_data": "Fetching matchup data",
        "search_site_data": "Searching site data",
    }

    for _step in range(max_steps):
        payload: dict = {
            "model": model or _get_model(),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or _get_max_tokens(),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _OPENAI_API_URL,
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = ""
            yield {"type": "error", "text": f"HTTP {exc.code}: {error_body[:200]}"}
            return
        except Exception as exc:
            yield {"type": "error", "text": str(exc)}
            return

        choices = body.get("choices") or []
        if not choices:
            yield {"type": "error", "text": "No choices in response."}
            return

        choice = choices[0]
        finish_reason = choice.get("finish_reason", "")
        asst_msg = choice.get("message") or {}
        tool_calls = asst_msg.get("tool_calls") or []

        # Final answer — model is done
        if finish_reason == "stop" or not tool_calls:
            content = _strip_think_tags(asst_msg.get("content") or "")
            yield {"type": "text", "text": content}
            yield {"type": "done"}
            return

        # Execute each tool call, yielding start/end events in real time
        messages.append(asst_msg)
        for call in tool_calls:
            call_id = call.get("id", "")
            fn = call.get("function") or {}
            fn_name = fn.get("name", "")
            try:
                fn_args = json.loads(fn.get("arguments", "{}") or "{}")
            except json.JSONDecodeError:
                fn_args = {}

            # Build a descriptive label for the UI
            label = _tool_labels.get(fn_name, fn_name.replace("_", " ").title())
            if fn_name == "get_matchup_data":
                ta = fn_args.get("team_a_name", "")
                tb = fn_args.get("team_b_name", "")
                if ta or tb:
                    label += f" \u00b7 {ta or '?'} vs {tb or '?'}"
            elif fn_name == "search_site_data":
                q = fn_args.get("query", "")
                if q:
                    label += f" \u00b7 {q[:50]}"

            yield {"type": "tool_start", "name": fn_name, "label": label}
            try:
                result = tool_executor(fn_name, fn_args)
            except Exception as exc:
                result = f"Tool error: {exc}"
            yield {"type": "tool_end", "name": fn_name, "label": label}

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": str(result),
            })

    yield {"type": "done"}


# Tool schemas exposed to the OpenAI model
_AGENT_TOOLS: list[dict] = [
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
                "Works for any team or player by name — no opponent required."
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


_PERSONA_BLOCK = """\
You are the Analyst -- your team's AI-powered scrim analyst for Marvel Rivals. \
You are embedded in the team's workflow: coaches and players come to you to make sense \
of their data, spot patterns they'd miss, and prep for matches.

## Personality
- **Analytical first, always.** Every insight is grounded in data. You never speculate without numbers to back it up.
- **Direct but warm.** You're the teammate who tells it like it is, but in a way that makes people want to improve. Lead with the insight, not the caveat.
- **Contextual.** Don't just say "62%% win rate" -- say "62%% win rate, which is a solid improvement from the 48%% you were sitting at two weeks ago." Numbers mean more with context.
- **Opinionated when the data supports it.** If the numbers clearly point to something, say so confidently. "You should probably keep running Luna Snow -- the data makes a strong case for it."
- **Celebrate wins.** When a player is popping off or the team is trending up, acknowledge it. Good coaching reinforces what's working, not just what's broken.
- **Concise.** Respect people's time. Lead with the headline, then supporting evidence. Skip the preamble.

## Marvel Rivals Context
- Marvel Rivals is a 6v6 team-based hero shooter featuring Marvel characters.
- A "scrim" (scrimmage) is a practice match between two teams, typically consisting of multiple maps.
- Roles: **Vanguard** (Tank), **Duelist** (Damage), **Strategist** (Support).
- There is **no role lock** in Marvel Rivals. Treat role counts as strategic comp choices, not forced constraints.
- Current common optimal structures include **2-2-2**, **triple tank**, and **triple support** depending on matchup, map, and comfort.
- Map types:
  - **Domination**: Capture and hold control points.
  - **Convoy**: Escort a payload to the objective.
  - **Convergence**: Capture a point, then escort it (Hybrid-style).
  - **Flash Point**: Flip multiple capture points across the map.
- **Draft system**: Each map has a structured draft phase with **4 bans** and **2 protects** per team. \
Order: first ban → first protect → second ban → third ban → second protect → fourth ban. \
Bans remove heroes from the pool; protects lock heroes in for your team and make them unbannable by the opponent.
- **Team-Up abilities**: Certain hero combinations unlock bonus passive or active abilities. \
These can be a strong reason to keep a duo protected or paired together in a comp.
- Key performance metrics are normalized to **per-10 minutes** for fair comparison across different map lengths.

## Key Metrics Vocabulary
- **K/D ratio**: Kills (eliminations) divided by deaths.
- **Per-10 stats**: Stats normalized to per-10 minutes of play (e.g., eliminations/10, deaths/10, damage/10).
- **First pick**: Getting the first kill in a team fight -- initiating advantage.
- **First death**: Being the first player to die in a team fight -- creating a disadvantage.
- **Ultimate economy**: How efficiently a team charges and uses ultimates.
- **Fight win rate**: Percentage of team fights won.
- **Z-score**: How far a stat deviates from the average for that hero (positive = above average, negative = below average).
- **Outlier**: A stat significantly above or below the hero average (|z-score| > 1.5).
- **Kill share**: Percentage of the team's final blows attributed to a single player -- a high number may signal over-reliance on one carry.
- **Protect value**: Whether a hero that was protected appeared in the comp and contributed to a win -- confirms the protect was well-placed.
- **MVP Score**: Composite score based on z-scores across multiple stats, weighted by importance.

## Coaching Philosophy
- A hero with high comfort and moderate win rate is often more important than one with a high win rate and few maps.
- Ban priority is driven by leverage, not just win rate. A hero banned 8 times is being suppressed for a reason.
- Pivot risk is the real cost of a ban. Forcing a player onto 3 appearances at 30%% WR is worth more than banning a hero with a strong backup.
- **Team-Up awareness**: When a hero is heavily protected or consistently paired with another, check if a Team-Up ability is a factor. Protecting a duo can lock in a power spike the opponent cannot disrupt.
- Confidence must match sample size. Call it uncertain when data is thin.

## Reasoning Order

For every draft question, work through this sequence -- do not skip steps:

1. **Identify state** -- map, side, bans used, protects used, heroes still open, opponent comforts open, our comforts open.
2. **Find the pressure point** -- choose ONE: deny comfort | protect win condition | force weak pivot | remove contested hero | break comp core | cover map weakness.
3. **Check the evidence** -- ban rate, protect rate, comfort rate, WR, map WR, pair data, Team-Up participation, pivot history, sample size.
4. **Make the call** -- single best move, one sentence why, one sentence risk, confidence label.
5. **Win rate alone is never enough** -- cross-check with ban/protect frequency and pivot quality before committing.

## Recommendation Labels

Every recommendation must be one of:
- **Comfort Denial** -- banning a hero the opponent relies on
- **Win Condition Protection** -- locking in our key hero early
- **Pivot Punish** -- forcing the enemy onto a weaker backup
- **Core Break** -- removing the hero that enables their preferred comp
- **Map Leverage** -- hero strength tied to this specific map
- **Contested Priority** -- both teams want it; act first
- **Team-Up Anchor** -- protecting or banning to control a key Team-Up pairing
- **Risk Control** -- data is thin; take the safer option

## Output Format

**Best Move:** [hero -- ban / protect / pick]
**Type:** [label from above]
**Reason:** [1 sentence with numbers]
**Draft Logic:** [how this changes opponent options]
**Risk:** [what could go wrong]
**Confidence:** High / Medium / Low

Internal slot labels (ban1, protect1, team1) must never appear in output. \
Translate: "first ban", "first protect", "your team", "the opponent". \
Never expose raw tags, ability slot numbers, fight phase enum values, or other internal data labels. \
Translate everything into natural language.
"""


def build_draft_system_prompt(
    context_text: str,
    site_context_text: str,
    personal_team: str,
    meta: dict | None = None,
    intent: str = "general",
    hero_names: list[str] | None = None,
) -> str:
    """
    Build the full system prompt for a Marvel Rivals draft advisory question.

    Layers (in order):
      1. _PERSONA_BLOCK        — HOW TO THINK (personality, reasoning, format)
      2. Team identity line    — who "we" means
      3. Hero theory context   — WHAT the relevant heroes do (from hero_theory.py)
                                  Injected only when hero_names are provided or
                                  the intent is hero/ban/comp focused.
      4. Live matchup data     — the pre-computed matchup context from the DB
      5. Site data             — player pools, hero stats, scrim history
      6. Question focus hint   — one sentence intent guide for this specific question

    hero_names: optional list of hero names whose theory profiles should be injected.
                If None and intent is ban/comp/pivot/contested, the function
                attempts to extract relevant heroes from the context data.
    """
    meta = meta or {}
    has_matchup = bool(meta.get("has_matchup"))
    team_a = meta.get("team_a") or personal_team or "our team"
    team_b = meta.get("team_b") or "the opponent"

    parts = [_PERSONA_BLOCK]

    if personal_team:
        parts.append(
            f'When the user says "we", "our team", or "us", they mean **{personal_team}**.'
        )

    # ── Hero theory injection ──────────────────────────────────────────────
    # Only inject for intents where hero knowledge changes the answer.
    _theory_intents = {"ban", "protect", "comp", "enemy_comps", "ban_impact",
                       "player_pivot", "hero_volatility", "pivot", "comfort", "contested"}
    if intent in _theory_intents or hero_names:
        try:
            from draft_engine.hero_theory import get_heroes_for_prompt
            names_to_describe = list(hero_names or [])
            if not names_to_describe and meta.get("hero_focus"):
                names_to_describe = [meta["hero_focus"]]
            if names_to_describe:
                theory_block = get_heroes_for_prompt(names_to_describe)
                if theory_block:
                    parts.append("## Hero Reference\n" + theory_block)
        except Exception:
            pass  # never break prompt building because of a missing theory file

    # ── Live matchup data ──────────────────────────────────────────────────
    if has_matchup and context_text.strip():
        parts.append(
            f"## Live Matchup Data: {team_a} vs {team_b}\n"
            + context_text.strip()
        )
    elif context_text.strip():
        parts.append("## Context Data\n" + context_text.strip())

    # ── Site data (player pools, hero stats, scrim history) ───────────────
    if site_context_text.strip():
        parts.append("## Team / Player / Hero Site Data\n" + site_context_text.strip())

    # ── Question focus hint ────────────────────────────────────────────────
    _intent_hints = {
        "ban":            "Lead with the top ban target and the leverage score that supports it.",
        "protect":        "Lead with the highest-value protect and why the data points there.",
        "comp":           "Lead with the strongest comp path and what anchors it.",
        "enemy_comps":    "Lead with the most likely opponent lineup and what enables it.",
        "ban_impact":     "Lead with how opponent options shift after the ban, and what we gain.",
        "player_pivot":   "Lead with the most likely pivot and the appearance count backing it.",
        "slot_compare":   "Lead with which ban slot shows the better outcome and by how much.",
        "hero_volatility":"Lead with which side the hero favors and why.",
        "risk":           "Lead with the highest-variance swing piece and how to neutralise it.",
        "map":            "Lead with the strongest map options and any comp dependencies.",
        "confidence":     "Lead with the confidence level and the sample size behind it.",
        "pivot":          "Lead with the most likely pivot path and our cleanest counter.",
        "comfort":        "Lead with the highest-impact comfort picks for each side.",
        "contested":      "Lead with the hero both teams most want and who benefits more.",
    }
    hint = _intent_hints.get(intent, "")
    if hint:
        parts.append(f"## Current Question Focus\n{hint}")

    return "\n\n".join(parts)
