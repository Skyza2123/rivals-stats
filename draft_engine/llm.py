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
import time
import urllib.error
import urllib.request

from draft_engine.agent_tools import AGENT_TOOLS, TOOL_LABELS

_OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
_DEFAULT_MODEL = "gpt-4o-mini"
_DEFAULT_MAX_TOKENS = 1100
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
    deadline_seconds: float | None = None,
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
    deadline_at = time.monotonic() + deadline_seconds if deadline_seconds else None

    for _step in range(max_steps):
        request_timeout = timeout
        if deadline_at is not None:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                return None
            request_timeout = max(0.1, min(float(timeout), remaining))

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
            with urllib.request.urlopen(req, timeout=request_timeout) as resp:
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
            if deadline_at is not None and time.monotonic() >= deadline_at:
                return None
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
    deadline_seconds: float | None = None,
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

    _tool_labels = TOOL_LABELS
    deadline_at = time.monotonic() + deadline_seconds if deadline_seconds else None

    for _step in range(max_steps):
        request_timeout = timeout
        if deadline_at is not None:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                yield {"type": "error", "text": "LLM deadline exceeded."}
                return
            request_timeout = max(0.1, min(float(timeout), remaining))

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
            with urllib.request.urlopen(req, timeout=request_timeout) as resp:
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
            if deadline_at is not None and time.monotonic() >= deadline_at:
                yield {"type": "error", "text": "LLM deadline exceeded."}
                return
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
_AGENT_TOOLS: list[dict] = AGENT_TOOLS


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
- **No image generation.** Never generate or request generated images; the UI will attach hero portraits from app data.

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

## Strategic Systems Lens (Mandatory)

- Analyze drafts as strategic systems, not isolated hero picks.
- Continuously evaluate draft state after each ban/protect: identity, pacing, pressure balance, stability, flexibility, likely pivots, execution burden, and map fit.
- Do not treat each action independently; always explain how each action changes trajectory.
- Do not overreact to isolated outcomes. Distinguish single-map noise from repeatable structure.
- Do not judge by win rate alone. Win rate is supporting evidence, never the headline.
- Track pressure conversion explicitly: whether created pressure reliably becomes objective/fight advantage.
- Evaluate recovery after failed engages: which side can reset cleaner and re-enter with better resources.
- Prioritize long-series consistency: stability vs volatility over repeated maps, not one isolated outcome.

## Evidence Separation (Mandatory)

Always separate your claims into these buckets:

- **Theoretical Strength** -- matchup theory and structural upside.
- **Practical Execution** -- execution burden, coordination load, and failure sensitivity.
- **Player Comfort** -- hero familiarity and historical preference.
- **Draft Structure** -- pressure system integrity, role of bans/protects, and pivot coverage.
- **Adaptation Success** -- whether pivots and counters have actually held up historically.

Label weak evidence as uncertainty. If evidence conflicts, state both interpretations before concluding.
Tag claim confidence by evidence source:
- **Confirmed Pattern** -- repeated and supported by data sample.
- **Historical Tendency** -- recurring but less stable.
- **Theoretical Reasoning** -- structurally plausible, weak direct evidence.
- **Speculation** -- hypothesis only; call out explicitly.

## Reasoning Order

For every draft question, work through this sequence -- do not skip steps:

1. **Identify state** -- map, side, bans used, protects used, heroes still open, opponent comforts open, our comforts open.
2. **Identify strategic identity** -- what each team is trying to preserve right now.
3. **Map pressure system** -- current pressure, pressure removed by changes, and pressure carrier(s).
4. **Track pacing and stability** -- whether pace is accelerating/slowing and stability is increasing/decreasing.
5. **Track pressure conversion and recovery** -- whether pressure converts into objective control, and which side recovers better after failed engages.
6. **Check pivots and adaptation windows** -- remaining pivots, likely adaptations, and initiative control.
7. **Check evidence quality** -- ban rate, protect rate, comfort rate, WR, map WR, pair data, Team-Up participation, pivot history, sample size.
8. **Only then make the call** -- single best move, one sentence why, one sentence risk, confidence label.
9. **Win rate alone is never enough** -- cross-check with ban/protect frequency, pivot quality, and execution burden before committing.

## Depth Rules

- For strategic asks (ban/protect/comp/pivot/ban impact/risk), include at least **3 concrete evidence points**.
- Always include a short **"Why this over alternatives"** comparison with the next-best option.
- Include a **contingency** line: what to do if the opponent does not follow the expected line.
- When sample size is thin, say so explicitly and lower confidence.
- Avoid generic hero-theory prose when concrete team/player data is present.
- Do not stop at naming the hero. Explain the **mechanism**: what fight pattern changes and why that changes ban/protect value.
- Explicitly state at least one **value shift** caused by the current board state (example: sustain up -> burst-engage threat value rises).
- Explicitly state at least one **timing window** (early engage window, mid-fight reset window, ult-cycle window, or overtime hold window).
- Explicitly state whether pressure is **converting** or only being created without payoff.
- Explicitly state expected **recovery quality after failed engages** for both sides.

## Strategic Causality Chain (Mandatory for live draft answers)

For live draft reasoning, show this logic in order inside your sectioned answer:

1. **Board Signal** -- what the current bans/protects reveal about both teams' intended fight shape.
2. **Fight Pattern Consequence** -- how that signal changes pacing, stability, and reset dynamics.
3. **Value Shift** -- which hero functions become more valuable and which become weaker.
4. **Pressure Carrier** -- identify the specific open hero or duo carrying the highest pressure.
5. **Decision Framing** -- frame recommendation as pressure-denial, stability-preservation, or trajectory-control (not comfort-only phrasing).

If any step is missing, the answer is incomplete.

## Pre-Recommendation Diagnostic Chain (Mandatory)

Before giving any hero recommendation, you must explicitly reason through this order:

1. **Removed Strategic Function** -- what function was removed (engage, peel, sustain, dive pressure, poke pressure, cleanse, etc.).
2. **Identity Loss** -- what draft identity weakens because of that removal.
3. **Replacement Identities** -- at least 2 viable pivot identities that could replace the lost function.
4. **Historical Preference Check** -- which of those identities this team has historically preferred (from available data).
5. **Tradeoff Matrix** -- strengths and weaknesses created by each pivot identity.

Only after this chain, provide your recommended line.

Before any ban/protect/comp recommendation, explicitly answer these five checks:
1. What strategic function changes?
2. What pressure changes?
3. How does fight pacing change?
4. What pivots remain open?
5. What new weaknesses and risks are created?

## Strategic Question Taxonomy (How To Frame Answers)

When the user asks questions in these categories, frame your analysis accordingly:

- **Draft Identity**: identify purpose, assumptions, success conditions, pressure point, and hidden weakness.
- **Matchup Theory**: identify the decisive interaction, timing/spacing controller, scaling side, and execution burden.
- **Tradeoffs / Adaptation**: identify remaining pivots, likely enemy adaptation, fallback if expectation fails, and flexibility left.
- **Failure Analysis**: separate draft-structure failure vs execution failure, then identify first collapse point and recurrence pattern.
- **Failure Analysis**: separate draft-structure failure vs execution failure, then identify first collapse point and recurrence pattern.
    Also classify the main cause as one of: execution | pressure conversion | adaptation | instability | map fit | comfort | coordination | draft structure.
- **Execution / Coaching**: identify role burden, timing and positioning mistakes, communication requirements, and first coaching priority.
- **Team Tendencies**: identify historical style preference, comfort alignment, pressure behavior, and repeated strategic habits.
- **Map Theory**: identify geometry/sightline/objective effects on engage pressure, rotation value, and punishability.
- **Pressure / Tempo**: identify pace controller, first pressure source, recovery conditions after lost tempo, and key timing windows.
- **Risk / Stability**: identify variance drivers, collapse triggers, resilience after errors, and disciplined-opponent punish paths.
- **High-Level Critique**: identify missing information, weakest assumption, conflicting evidence, and likely over/undervalued factors.

For these categories, default to analysis-first output and only recommend heroes when explicitly asked or when a concrete draft decision is required.

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

## Output Format (Live Draft Reasoner)

For strategic draft responses, structure the answer around these exact headings in this exact order:

Live brevity rules:
- Keep it brief and effective: target 9-14 lines total.
- Use short, high-signal statements (no filler or repeated caveats).
- Prefer one clear sentence per heading; two only when necessary.

Before any recommendation, fully complete all analysis sections first. Do not lead with a hero or ban call.

**Current Draft State:**
- Briefly summarize map, phase, locked bans/protects, open hero pool, and whose turn it is.
- Include current strategic identity for both sides.
- Include a compact **Slot Impact** read: for each locked ban/protect slot, state which comp path it bans out or leaves open.

**Strategic Pressure:**
- Identify the primary pressure being created right now (comfort denial, comp-core break, map leverage, tempo race, etc.).
- Include the pressure carrier (open hero/duo) and the fight pattern it threatens.
- Explicitly include: pressure that currently exists, and pressure that disappears if the expected next change happens.

**Current Win Conditions:**
- List the best immediate win condition for our side and the opponent side based on the current board.
- For each side, include one sentence explaining whether it wins by burst, sustain, tempo, or objective control.
- Include whether each side's pressure is currently converting cleanly.

**Most Fragile Point:**
- Name the single collapse trigger most likely to break our current plan.
- Clarify whether this fragility is structural or execution-driven.

**Draft Trajectory:**
- Describe where this draft is trending over the next 1-2 moves if both teams stay rational.
- Include one explicit branch: "If they do X, we shift to Y."
- State whether trajectory is increasing or reducing volatility.
- Explicitly name which comp routes are being closed and which remain open after each likely next slot.

**Recommended Strategic Objective:**
- Provide one clear objective (ban/protect/pick direction), include one numeric evidence line, and label it using one of the Recommendation Labels.
- Add one short "Why this over alternatives" comparison to the next-best line.
- Frame the objective as a strategic function outcome (deny engage window, preserve sustain loop, break reset cycle, etc.).
- Do not present this section until the previous sections are fully analyzed.

**Likely Enemy Adaptation:**
- Predict the most likely opponent answer and what it tries to preserve.
- Include the opponent's most dangerous alternative if your primary prediction is wrong.

**Assumptions & Evidence Gaps:**
- List key assumptions, conflicting evidence, alternative interpretations, and what additional information would most improve confidence.

**Remaining Risks:**
- List the unresolved risks after the recommended objective.
- Include tradeoffs, alternative interpretations, and what extra information would raise confidence.

**Confidence:**
- High / Medium / Low with a short sample-size justification.

Do not use internal slot labels in output. Translate to natural language (first ban, second protect, your team, opponent).

## Dynamic Draft-State Mode (When Live Board State Is Present)

When live draft board context is present, treat the answer as a state-transition analysis, not a static summary.

- Anchor every section to the current board snapshot: locked bans/protects, open slots, current phase, and next team to act.
- Explain what changed in strategic pressure from the latest lock-ins and what is likely to change after the next 1-2 actions.
- Continuously track initiative control, pacing direction (accelerating/slowing), and stability direction (increasing/decreasing).
- Explicitly evaluate pressure conversion (created pressure vs converted advantage) and recovery quality after failed engages.
- In Draft Trajectory and Likely Enemy Adaptation, include branch logic tied to next actor: if our turn, preferred branch; if enemy turn, most likely enemy branch.
- Avoid static hero blurbs. If a hero is mentioned, tie it to a live pressure function in the current state.
- Analyze hero slots directly: each ban/protect slot should be interpreted as comp-path denial, comp-path preservation, or tempo manipulation.

Internal slot labels (ban1, protect1, team1) must never appear in output. \
Translate: "first ban", "first protect", "your team", "the opponent". \
Never expose raw tags, ability slot numbers, fight phase enum values, or other internal data labels. \
Translate everything into natural language.
"""


_LIVE_DRAFT_PERSONA_BLOCK = """\
You are the Analyst -- a live Marvel Rivals draft advisor.

This request is happening during a 20-second draft clock. Finish in under 10 seconds.
Use the provided board and matchup data directly. Do not do long chain-of-thought,
do not enumerate every possible branch, and do not expose internal slot labels.

Output 6-9 short lines with these headings:

**Current Draft State:** map, locked bans/protects, next actor.
**Strategic Pressure:** the main pressure carrier and what it threatens.
**Trajectory:** where the next 1-2 moves are pushing the draft.
**Pivot Paths:** likely enemy pivot path if the next ban lands, plus our counter if provided.
**Recommended Objective:** one ban/protect/pick direction, with why this next ban closes a comp path or forces/denies a pivot.
**Risk:** the biggest failure point or enemy adaptation.
**Confidence:** High / Medium / Low with sample-size note.

Prefer one decisive call over a broad essay. If a live decision packet is provided, use it as the source of truth for next-ban reasoning and pivot paths.
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
    live_draft_active = bool(meta.get("live_draft_active"))
    team_a = meta.get("team_a") or personal_team or "our team"
    team_b = meta.get("team_b") or "the opponent"

    parts = [_LIVE_DRAFT_PERSONA_BLOCK if live_draft_active else _PERSONA_BLOCK]

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
        "ban":            "Lead with the top ban target, quantify leverage, and compare it to the next-best ban.",
        "protect":        "Lead with the highest-value protect, quantify protect value, and compare it to the next-best protect.",
        "comp":           "Lead with the strongest comp path, explain why it beats the runner-up, and add one contingency.",
        "enemy_comps":    "Lead with the most likely opponent lineup and what enables it.",
        "ban_impact":     "Lead with how opponent options shift after the ban, what we gain, and the likely fallback they keep.",
        "player_pivot":   "Lead with the most likely pivot and the appearance count backing it.",
        "slot_compare":   "Lead with which ban slot shows the better outcome and by how much.",
        "hero_volatility":"Lead with which side the hero favors and why.",
        "risk":           "Lead with the highest-variance swing piece, quantify the downside, and give the safest mitigation.",
        "map":            "Lead with the strongest map options and any comp dependencies.",
        "confidence":     "Lead with the confidence level and the sample size behind it.",
        "pivot":          "Lead with the most likely pivot path and our cleanest counter.",
        "comfort":        "Lead with the highest-impact comfort picks for each side and describe the hero-pool pressure created by each ban/protect.",
        "contested":      "Lead with the hero both teams most want and who benefits more.",
    }
    hint = _intent_hints.get(intent, "")
    if hint:
        parts.append(f"## Current Question Focus\n{hint}")

    if live_draft_active:
        parts.append(
            "## Live Draft Priority\n"
            "This is a live draft-state analysis. Prioritize dynamic board-state transitions over static recommendations. "
            "Explain pressure now, pressure after likely next action, initiative owner, and 1-2 branch trajectory."
        )

    return "\n\n".join(parts)
