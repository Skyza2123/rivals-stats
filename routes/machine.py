# pyright: reportUndefinedVariable=false
# pylint: disable=undefined-variable
# flake8: noqa
# ruff: noqa: F821
# Transitional module executed in app.py's namespace.

_DRAFT_REASONER_MODEL_CACHE: dict[tuple, dict] = {}
_DRAFT_REASONER_MODEL_CACHE_TTL_SECONDS = 180
_DRAFT_REASONER_MODEL_CACHE_MAX_ITEMS = 32
_MACHINE_CHAT_RESPONSE_CACHE: dict[tuple, dict] = {}
_MACHINE_CHAT_RESPONSE_CACHE_TTL_SECONDS = 60
_MACHINE_CHAT_RESPONSE_CACHE_MAX_ITEMS = 96

MACHINE_DRAFT_METHOD_MAP = {
    "ban_significance": "multifactor_anova",
    "hero_interactions": "graph_network_models",
    "draft_prediction": "gradient_boosting",
    "pick_sequencing": "markov_tree_search",
    "synergy_clustering": "embeddings_kmeans",
    "volatile_bans": "permutation_importance",
}


def _draft_reasoner_cache_prune(now_ts: float) -> None:
    expired = [
        key
        for key, item in _DRAFT_REASONER_MODEL_CACHE.items()
        if (now_ts - float(item.get("ts") or 0)) > _DRAFT_REASONER_MODEL_CACHE_TTL_SECONDS
    ]
    for key in expired:
        _DRAFT_REASONER_MODEL_CACHE.pop(key, None)
    if len(_DRAFT_REASONER_MODEL_CACHE) <= _DRAFT_REASONER_MODEL_CACHE_MAX_ITEMS:
        return
    # Keep most recently used entries when over capacity.
    by_age = sorted(
        _DRAFT_REASONER_MODEL_CACHE.items(),
        key=lambda kv: float((kv[1] or {}).get("ts") or 0),
        reverse=True,
    )
    _DRAFT_REASONER_MODEL_CACHE.clear()
    for key, value in by_age[:_DRAFT_REASONER_MODEL_CACHE_MAX_ITEMS]:
        _DRAFT_REASONER_MODEL_CACHE[key] = value


def _machine_chat_response_cache_prune(now_ts: float) -> None:
    expired = [
        key
        for key, item in _MACHINE_CHAT_RESPONSE_CACHE.items()
        if (now_ts - float(item.get("ts") or 0)) > _MACHINE_CHAT_RESPONSE_CACHE_TTL_SECONDS
    ]
    for key in expired:
        _MACHINE_CHAT_RESPONSE_CACHE.pop(key, None)
    if len(_MACHINE_CHAT_RESPONSE_CACHE) <= _MACHINE_CHAT_RESPONSE_CACHE_MAX_ITEMS:
        return
    by_age = sorted(
        _MACHINE_CHAT_RESPONSE_CACHE.items(),
        key=lambda kv: float((kv[1] or {}).get("ts") or 0),
        reverse=True,
    )
    _MACHINE_CHAT_RESPONSE_CACHE.clear()
    for key, value in by_age[:_MACHINE_CHAT_RESPONSE_CACHE_MAX_ITEMS]:
        _MACHINE_CHAT_RESPONSE_CACHE[key] = value

@app.route("/draft-simulator")
def draft_simulator():
    teams = get_db().execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    return render_template(
        "draft_simulator.html",
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        teams=teams,
    )


@app.route("/draft-reasoner")
def draft_reasoner():
    # Keep legacy URL working, but route everyone into the unified Machine UI.
    return redirect(url_for("machine", tab="reasoner"))


@app.route("/machine")
def machine():
    db = get_db()
    initial_tab = (request.args.get("tab") or "matchup").strip().lower()
    if initial_tab not in {"matchup", "reasoner", "chat"}:
        initial_tab = "matchup"
    teams = db.execute("SELECT id, name FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    personal_team = db.execute(
        "SELECT id, name FROM teams WHERE is_personal = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    machine_history = SCRIMS + TOURNAMENT_MATCHES
    season_options = get_scrim_season_options(machine_history)
    default_season = get_current_season_from_recent_scrim(machine_history)
    has_unseasoned = any(not normalize_season_value(s.get("season", "")) for s in machine_history)
    return render_template(
        "machine.html",
        initial_tab=initial_tab,
        hero_roles=HERO_ROLES,
        hero_transformations=HERO_TRANSFORMATIONS,
        teams=teams,
        maps=MAPS,
        season_options=season_options,
        default_season=default_season,
        has_unseasoned=has_unseasoned,
        unspecified_season_token=UNSPECIFIED_SEASON_TOKEN,
        personal_team=personal_team,
    )


def _machine_chat_join(values: list[str], limit: int = 5) -> str:
    seen = set()
    cleaned = []
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return ", ".join(cleaned[:limit]) if cleaned else "none"


def _machine_chat_row_list(rows: list[dict], formatter, limit: int = 5) -> str:
    parts = []
    for row in (rows or [])[:limit]:
        try:
            text = formatter(row)
        except (TypeError, ValueError, KeyError):
            text = ""
        if text:
            parts.append(text)
    return "; ".join(parts) if parts else "none"


def _machine_hero_role(hero_name: str) -> str:
    try:
        from draft_engine.hero_theory import get_hero_profile
        profile = get_hero_profile(hero_name) or {}
        role = str(profile.get("role") or "").strip()
        if role:
            return role
    except Exception:
        pass
    for role_name, heroes in HERO_ROLES.items():
        if any(str(hero).lower() == str(hero_name or "").strip().lower() for hero in heroes):
            return role_name
    return ""


def _machine_build_ban_anova_rows(team_history: list[dict], candidate_heroes: list[str]) -> dict[str, dict]:
    """Estimate ban significance with a multifactor ANOVA-style partial F test.

    Response is opponent loss rate from this team's perspective. Predictors are:
    candidate ban presence + map mode + map name + submap + round + source + side. This keeps the
    ban signal from being credited for context effects like map pool or data source.
    """
    try:
        import numpy as np
    except Exception:
        return {}

    candidate_keys = {
        _canonical_draft_hero(hero).lower(): _canonical_draft_hero(hero)
        for hero in candidate_heroes or []
        if _canonical_draft_hero(hero)
    }
    if not candidate_keys:
        return {}

    rows = []
    for scrim in team_history or []:
        source = "tournament" if (scrim.get("source") or scrim.get("event") or scrim.get("tournament_name")) else "scrim"
        for map_entry in scrim.get("maps", []) or []:
            team_slot = map_entry.get("our_team_slot", "team1")
            if team_slot not in TEAM_SLOTS:
                team_slot = "team1"
            outcome = get_map_outcome_for_slot(map_entry, team_slot)
            if outcome not in {"Win", "Loss"}:
                continue
            draft = map_entry.get("draft", {})
            if not isinstance(draft, dict):
                continue
            opponent_slot = opposite_team_slot(team_slot)
            opponent_draft = draft.get(opponent_slot, {})
            if not isinstance(opponent_draft, dict):
                continue
            opponent_bans = {
                _canonical_draft_hero(opponent_draft.get(slot_key, "")).lower()
                for slot_key in ("ban1", "ban2", "ban3", "ban4")
                if _canonical_draft_hero(opponent_draft.get(slot_key, ""))
            }
            if not opponent_bans:
                continue
            map_name = (map_entry.get("map_name") or map_entry.get("map") or "").strip() or "Unknown"
            section_rows = [section for section in map_entry.get("comp", []) if isinstance(section, dict)]
            submap_labels = []
            for section in section_rows:
                submap_name = (section.get("submap") or "").strip()
                if submap_name and submap_name not in submap_labels:
                    submap_labels.append(submap_name)
            rows.append(
                {
                    "loss": 1.0 if outcome == "Loss" else 0.0,
                    "bans": opponent_bans,
                    "mode": MAP_MODES.get(map_name, "Other"),
                    "map": map_name,
                    "submap": submap_labels[0] if submap_labels else "Unknown Submap",
                    "round": str(len(section_rows) or 1),
                    "source": source,
                    "side": team_slot,
                }
            )

    if len(rows) < 8:
        return {}

    def build_matrix(items: list[dict], include_ban: bool, hero_key: str):
        columns = [[1.0] * len(items)]
        if include_ban:
            columns.append([1.0 if hero_key in row["bans"] else 0.0 for row in items])
        for factor in ("mode", "map", "submap", "round", "source", "side"):
            values = sorted({str(row.get(factor) or "") for row in items})
            if len(values) <= 1:
                continue
            for value in values[1:]:
                columns.append([1.0 if str(row.get(factor) or "") == value else 0.0 for row in items])
        return np.asarray(columns, dtype=float).T

    y = np.asarray([row["loss"] for row in rows], dtype=float)

    def sse_for(matrix) -> tuple[float, int]:
        beta, *_ = np.linalg.lstsq(matrix, y, rcond=None)
        residuals = y - matrix.dot(beta)
        rank = int(np.linalg.matrix_rank(matrix))
        return float(np.sum(residuals ** 2)), max(0, len(y) - rank)

    results = {}
    for hero_key, hero_name in candidate_keys.items():
        present_count = sum(1 for row in rows if hero_key in row["bans"])
        absent_count = len(rows) - present_count
        if present_count < 2 or absent_count < 2:
            results[hero_key] = {
                "hero": hero_name,
                "sample": len(rows),
                "ban_present_maps": present_count,
                "ban_absent_maps": absent_count,
                "status": "insufficient",
                "significance": "insufficient sample",
            }
            continue
        try:
            full = build_matrix(rows, True, hero_key)
            reduced = build_matrix(rows, False, hero_key)
            sse_full, df_full = sse_for(full)
            sse_reduced, df_reduced = sse_for(reduced)
        except Exception:
            continue
        df_num = max(1, df_reduced - df_full)
        if df_full <= 0 or sse_full <= 0 or sse_reduced < sse_full:
            f_stat = 0.0
            partial_eta = 0.0
        else:
            ss_effect = max(0.0, sse_reduced - sse_full)
            f_stat = (ss_effect / df_num) / (sse_full / df_full)
            partial_eta = ss_effect / (ss_effect + sse_full) if (ss_effect + sse_full) else 0.0
        present_loss = sum(row["loss"] for row in rows if hero_key in row["bans"]) / present_count
        absent_loss = sum(row["loss"] for row in rows if hero_key not in row["bans"]) / absent_count
        lift = (present_loss - absent_loss) * 100.0
        if present_count < 3 or absent_count < 3:
            significance = "low sample"
        elif f_stat >= 6.8 and partial_eta >= 0.10:
            significance = "strong"
        elif f_stat >= 3.8 and partial_eta >= 0.05:
            significance = "moderate"
        elif f_stat >= 2.0 and partial_eta >= 0.025:
            significance = "weak"
        else:
            significance = "not significant"
        results[hero_key] = {
            "hero": hero_name,
            "sample": len(rows),
            "ban_present_maps": present_count,
            "ban_absent_maps": absent_count,
            "enemy_loss_when_banned": round(present_loss * 100, 1),
            "enemy_loss_when_open": round(absent_loss * 100, 1),
            "loss_lift_pp": round(lift, 1),
            "f_stat": round(f_stat, 3),
            "df_num": df_num,
            "df_den": df_full,
            "partial_eta_sq": round(partial_eta, 4),
            "significance": significance,
            "status": "ready",
            "factors": ["ban_presence", "map_mode", "map_name", "submap", "round", "source", "side"],
        }
    return results


def _machine_build_ban_permutation_importance(team_history: list[dict], candidate_heroes: list[str]) -> dict[str, dict]:
    """Estimate volatile-ban importance by permuting ban-presence features in a local linear model."""
    try:
        import numpy as np
    except Exception:
        return {}

    candidate_names = []
    seen = set()
    for hero in candidate_heroes or []:
        canonical = _canonical_draft_hero(hero)
        key = canonical.lower()
        if canonical and key not in seen:
            candidate_names.append(canonical)
            seen.add(key)
    if not candidate_names:
        return {}
    candidate_keys = [hero.lower() for hero in candidate_names]

    rows = []
    for scrim in team_history or []:
        source = "tournament" if (scrim.get("source") or scrim.get("event") or scrim.get("tournament_name")) else "scrim"
        for map_entry in scrim.get("maps", []) or []:
            team_slot = map_entry.get("our_team_slot", "team1")
            if team_slot not in TEAM_SLOTS:
                team_slot = "team1"
            outcome = get_map_outcome_for_slot(map_entry, team_slot)
            if outcome not in {"Win", "Loss"}:
                continue
            draft = map_entry.get("draft", {})
            if not isinstance(draft, dict):
                continue
            opponent_draft = draft.get(opposite_team_slot(team_slot), {})
            if not isinstance(opponent_draft, dict):
                continue
            opponent_bans = {
                _canonical_draft_hero(opponent_draft.get(slot_key, "")).lower()
                for slot_key in ("ban1", "ban2", "ban3", "ban4")
                if _canonical_draft_hero(opponent_draft.get(slot_key, ""))
            }
            if not opponent_bans:
                continue
            map_name = (map_entry.get("map_name") or map_entry.get("map") or "").strip() or "Unknown"
            rows.append(
                {
                    "loss": 1.0 if outcome == "Loss" else 0.0,
                    "bans": opponent_bans,
                    "mode": MAP_MODES.get(map_name, "Other"),
                    "map": map_name,
                    "source": source,
                    "side": team_slot,
                }
            )

    if len(rows) < 8:
        return {}

    columns = [[1.0] * len(rows)]
    candidate_column_indexes = {}
    for hero_key in candidate_keys:
        candidate_column_indexes[hero_key] = len(columns)
        columns.append([1.0 if hero_key in row["bans"] else 0.0 for row in rows])
    for factor in ("mode", "map", "source", "side"):
        values = sorted({str(row.get(factor) or "") for row in rows})
        if len(values) <= 1:
            continue
        for value in values[1:]:
            columns.append([1.0 if str(row.get(factor) or "") == value else 0.0 for row in rows])

    try:
        x = np.asarray(columns, dtype=float).T
        y = np.asarray([row["loss"] for row in rows], dtype=float)
        beta, *_ = np.linalg.lstsq(x, y, rcond=None)
        pred = x.dot(beta)
        baseline_mse = float(np.mean((y - pred) ** 2))
    except Exception:
        return {}

    results = {}
    for hero_name, hero_key in zip(candidate_names, candidate_keys):
        present_count = int(sum(1 for row in rows if hero_key in row["bans"]))
        absent_count = len(rows) - present_count
        col_idx = candidate_column_indexes.get(hero_key)
        if col_idx is None or present_count < 2 or absent_count < 2:
            results[hero_key] = {
                "hero": hero_name,
                "status": "insufficient",
                "sample": len(rows),
                "ban_present_maps": present_count,
                "ban_absent_maps": absent_count,
            }
            continue
        permuted = x.copy()
        shift = max(1, len(rows) // 2)
        permuted[:, col_idx] = np.roll(permuted[:, col_idx], shift)
        perm_pred = permuted.dot(beta)
        perm_mse = float(np.mean((y - perm_pred) ** 2))
        mse_lift = max(0.0, perm_mse - baseline_mse)
        relative_lift = (mse_lift / baseline_mse) if baseline_mse > 0 else 0.0
        if relative_lift >= 0.25:
            importance = "high"
        elif relative_lift >= 0.10:
            importance = "medium"
        elif relative_lift >= 0.04:
            importance = "low"
        else:
            importance = "minimal"
        results[hero_key] = {
            "hero": hero_name,
            "status": "ready",
            "method": "permutation_importance",
            "sample": len(rows),
            "ban_present_maps": present_count,
            "ban_absent_maps": absent_count,
            "baseline_mse": round(baseline_mse, 5),
            "permuted_mse": round(perm_mse, 5),
            "mse_lift": round(mse_lift, 5),
            "relative_lift": round(relative_lift, 4),
            "importance": importance,
            "factors": ["candidate_bans", "map_mode", "map_name", "source", "side"],
        }
    return results


def _machine_build_ban_candidate_details(
    candidate_heroes: list[str],
    *,
    a_model: dict,
    b_model: dict,
    matchup_model: dict,
    target_comp: list[str],
    enemy_comps: list[dict],
    ban_significance: dict[str, dict] | None = None,
    permutation_importance: dict[str, dict] | None = None,
) -> list[dict]:
    """Score ban candidates based on matchup statistics (frequencies, rates, histories)."""
    try:
        from draft_engine.hero_theory import get_hero_profile, get_hero_score
    except Exception:
        get_hero_profile = lambda _hero: None
        get_hero_score = lambda _hero: None

    priority_value = {"high": 18.0, "medium": 10.0, "low": 4.0}
    role_alias = {"Vanguard": "Tank", "Duelist": "DPS", "Strategist": "Support"}
    target_comp_keys = {str(hero or "").strip().lower() for hero in target_comp}

    def indexed(rows: list[dict]) -> dict[str, dict]:
        return {
            str(row.get("hero", "") or "").strip().lower(): row
            for row in rows or []
            if str(row.get("hero", "") or "").strip()
        }

    our_pool = indexed(a_model.get("hero_pool_rows", []))
    enemy_pool = indexed(b_model.get("hero_pool_rows", []))
    our_comfort = indexed(a_model.get("comfort_core_rows", []))
    enemy_comfort = indexed(b_model.get("comfort_core_rows", []))
    volatility = {
        str(row.get("hero", "") or "").strip().lower(): row
        for row in matchup_model.get("volatile_matchup_rows", []) or []
        if str(row.get("hero", "") or "").strip()
    }
    force_rows = matchup_model.get("force_matchup_rows", []) or []
    historical_ban_count: dict[str, int] = {}
    historical_ban_rate: dict[str, float] = {}
    for line in b_model.get("ban_line_rows", []) or []:
        line_count = int(line.get("count", 0) or 0)
        line_rate = float(line.get("rate", 0) or 0)
        for raw_ban in line.get("bans", []) or []:
            ban_key = str(raw_ban or "").strip().lower()
            if not ban_key:
                continue
            historical_ban_count[ban_key] = historical_ban_count.get(ban_key, 0) + line_count
            historical_ban_rate[ban_key] = historical_ban_rate.get(ban_key, 0.0) + line_rate

    max_historical_count = max(historical_ban_count.values()) if historical_ban_count else 0
    ban_significance = ban_significance or {}
    permutation_importance = permutation_importance or {}

    rows = []
    seen = set()
    for raw_hero in candidate_heroes or []:
        hero = str(raw_hero or "").strip()
        hero_key = hero.lower()
        if not hero or hero_key in seen:
            continue
        seen.add(hero_key)

        profile = get_hero_profile(hero) or {}
        scores = get_hero_score(hero) or {}
        role = str(profile.get("role") or "").strip() or _machine_hero_role(hero)
        ban_priority = str(profile.get("ban_priority") or "medium").strip().lower()
        strategic_score = float(scores.get("strategic_contribution") or 5)
        stability = float(scores.get("stability") or 5)
        engage = float(scores.get("engage_score") or 5)
        poke = float(scores.get("poke_score") or 5)
        peel = float(scores.get("peel_score") or 5)
        sustain = float(scores.get("sustain_score") or 5)
        execution_burden = float(scores.get("execution_burden") or scores.get("execution_difficulty") or 5)
        pressure_type = str(scores.get("pressure_type") or profile.get("archetype") or "").strip()

        enemy_row = enemy_pool.get(hero_key) or enemy_comfort.get(hero_key) or {}
        our_row = our_pool.get(hero_key) or our_comfort.get(hero_key) or {}
        enemy_dependency = (
            float(enemy_row.get("comfort_rate", 0) or 0) * 0.56
            + float(enemy_row.get("adjusted_win_rate", 0) or enemy_row.get("raw_win_rate", 0) or 0) * 0.24
            + float(enemy_row.get("profile_score", 0) or 0) * 0.20
        )
        our_dependency = (
            float(our_row.get("comfort_rate", 0) or 0) * 0.56
            + float(our_row.get("adjusted_win_rate", 0) or our_row.get("raw_win_rate", 0) or 0) * 0.24
            + float(our_row.get("profile_score", 0) or 0) * 0.20
        )

        comp_hits = []
        for comp in enemy_comps or []:
            comp_heroes = [str(h or "").strip() for h in comp.get("heroes", [])]
            if hero_key in {h.lower() for h in comp_heroes}:
                comp_hits.append(comp)
        top_comp_hit = comp_hits[0] if comp_hits else {}
        comp_path_impact = (
            float(top_comp_hit.get("rate", 0) or 0) * 0.42
            + float(top_comp_hit.get("win_rate", 0) or 0) * 0.24
            + float(top_comp_hit.get("confidence", 0) or 0) * 0.16
        ) if top_comp_hit else 0.0

        path_impact = 0.0
        for row in force_rows:
            if hero_key not in {str(h or "").strip().lower() for h in row.get("our_bans", [])}:
                continue
            path_impact = max(
                path_impact,
                float(row.get("enemy_blocked_count", 0) or 0) * 8.0
                + float(row.get("enemy_choice_gap", 0) or 0) * 0.9
                + (100.0 - float(row.get("enemy_preserved_ratio", 100) or 100)) * 0.22,
            )

        volatile = volatility.get(hero_key) or {}
        enemy_stat_impact = max(0.0, float(volatile.get("team_b_delta", 0) or 0)) + comp_path_impact
        our_stat_cost = max(0.0, float(volatile.get("team_a_delta", 0) or 0))
        if hero_key in target_comp_keys:
            our_stat_cost += 22.0
        elif our_comfort.get(hero_key):
            our_stat_cost += 11.0

        hero_pool_value = (
            strategic_score * 6.4
            + max(engage, poke, peel, sustain) * 2.2
            + priority_value.get(ban_priority, 10.0)
            + max(0.0, 8.0 - execution_burden) * 1.2
        )
        hero_hist_count = int(historical_ban_count.get(hero_key, 0) or 0)
        hero_hist_rate = float(historical_ban_rate.get(hero_key, 0.0) or 0.0)
        history_count_pct = (hero_hist_count / max_historical_count) if max_historical_count > 0 else 0.0
        historical_priority_score = (hero_hist_rate * 0.72) + (history_count_pct * 28.0)
        hero_pool_multiplier = 1.0 + max(0.0, min(0.42, hero_pool_value / 240.0))
        weighted_historical_priority = historical_priority_score * hero_pool_multiplier
        anova = ban_significance.get(hero_key) or {}
        permutation = permutation_importance.get(hero_key) or {}
        anova_bonus = 0.0
        if anova.get("status") == "ready" and float(anova.get("loss_lift_pp", 0) or 0) > 0:
            if anova.get("significance") == "strong":
                anova_bonus = min(18.0, float(anova.get("loss_lift_pp", 0) or 0) * 0.45)
            elif anova.get("significance") == "moderate":
                anova_bonus = min(12.0, float(anova.get("loss_lift_pp", 0) or 0) * 0.32)
            elif anova.get("significance") == "weak":
                anova_bonus = min(6.0, float(anova.get("loss_lift_pp", 0) or 0) * 0.18)
        permutation_bonus = 0.0
        if permutation.get("status") == "ready":
            if permutation.get("importance") == "high":
                permutation_bonus = min(14.0, float(permutation.get("relative_lift", 0) or 0) * 32.0)
            elif permutation.get("importance") == "medium":
                permutation_bonus = min(8.0, float(permutation.get("relative_lift", 0) or 0) * 22.0)
            elif permutation.get("importance") == "low":
                permutation_bonus = min(4.0, float(permutation.get("relative_lift", 0) or 0) * 12.0)
        strategic_enemy_value = enemy_dependency + enemy_stat_impact + path_impact + hero_pool_value * 0.26
        strategic_our_value = our_dependency + our_stat_cost + (hero_pool_value * 0.10 if hero_key in target_comp_keys else 0.0)
        strategic_net_value = strategic_enemy_value - strategic_our_value
        ban_score = (
            weighted_historical_priority * 1.05
            + strategic_enemy_value * 0.64
            + hero_pool_value * 0.24
            + anova_bonus
            + permutation_bonus
            - strategic_our_value * 0.52
            - max(0.0, stability - 7.0) * 1.8
        )

        strategic_caution = ""
        why_not_reason = ""
        if hero_key in target_comp_keys:
            strategic_caution = "costs our projected comp path"
            why_not_reason = "also removes one of our projected comp pieces"
        elif our_dependency >= enemy_dependency and our_dependency >= 22:
            strategic_caution = "high self-cost"
            why_not_reason = "our history values this hero as much as theirs"
        elif not enemy_row and not comp_hits and not path_impact:
            strategic_caution = "thin enemy signal"
            why_not_reason = "mostly theory-driven; limited opponent-specific evidence"

        if ban_score >= 76 and strategic_net_value > 18:
            model_hint = "excellent net candidate"
        elif strategic_net_value > 8:
            model_hint = "good net candidate"
        elif strategic_net_value > -4:
            model_hint = "situational candidate"
        else:
            model_hint = "costly candidate"

        rows.append(
            {
                "hero": hero,
                "role": role,
                "role_alias": role_alias.get(role, role),
                "archetype": profile.get("archetype", ""),
                "pressure_type": pressure_type,
                "ban_priority": ban_priority,
                "strategic_net_value": round(strategic_net_value, 1),
                "strategic_enemy_value": round(strategic_enemy_value, 1),
                "strategic_our_value": round(strategic_our_value, 1),
                "ban_score": round(ban_score, 1),
                "enemy_dependency": round(enemy_dependency, 1),
                "our_dependency": round(our_dependency, 1),
                "enemy_stat_impact": round(enemy_stat_impact, 1),
                "our_stat_cost": round(our_stat_cost, 1),
                "hero_pool_value": round(hero_pool_value, 1),
                "historical_ban_count": hero_hist_count,
                "historical_ban_rate": round(hero_hist_rate, 1),
                "weighted_historical_priority": round(weighted_historical_priority, 1),
                "path_impact": round(path_impact, 1),
                "anova_significance": anova,
                "anova_bonus": round(anova_bonus, 1),
                "permutation_importance": permutation,
                "permutation_bonus": round(permutation_bonus, 1),
                "strategic_caution": strategic_caution,
                "why_not_reason": why_not_reason,
                "model_hint": model_hint,
            }
        )

    rows.sort(
        key=lambda row: (
            float(row.get("weighted_historical_priority", 0) or 0),
            float(row.get("ban_score", 0) or 0),
            float(row.get("strategic_net_value", 0) or 0),
        ),
        reverse=True,
    )
    return rows


def _machine_build_protect_candidate_details(
    candidate_heroes: list[str],
    *,
    a_model: dict,
    b_model: dict,
    matchup_model: dict,
    target_comp: list[str],
    enemy_expected_bans: list[str],
) -> list[dict]:
    """Score protect candidates based on team statistics and play frequency."""
    try:
        from draft_engine.hero_theory import get_hero_profile, get_hero_score
    except Exception:
        get_hero_profile = lambda _hero: None
        get_hero_score = lambda _hero: None

    priority_value = {"high": 12.0, "medium": 8.0, "low": 4.0}
    role_alias = {"Vanguard": "Tank", "Duelist": "DPS", "Strategist": "Support"}
    target_comp_keys = {str(hero or "").strip().lower() for hero in target_comp}
    enemy_expected_ban_keys = {str(hero or "").strip().lower() for hero in enemy_expected_bans}

    def indexed(rows: list[dict]) -> dict[str, dict]:
        return {
            str(row.get("hero", "") or "").strip().lower(): row
            for row in rows or []
            if str(row.get("hero", "") or "").strip()
        }

    our_pool = indexed(a_model.get("hero_pool_rows", []))
    enemy_pool = indexed(b_model.get("hero_pool_rows", []))
    our_comfort = indexed(a_model.get("comfort_core_rows", []))
    enemy_comfort = indexed(b_model.get("comfort_core_rows", []))
    volatility = {
        str(row.get("hero", "") or "").strip().lower(): row
        for row in matchup_model.get("volatile_matchup_rows", []) or []
        if str(row.get("hero", "") or "").strip()
    }

    route_protects = set()
    for row in matchup_model.get("force_matchup_rows", []) or []:
        route_protects.update(str(hero or "").strip().lower() for hero in row.get("our_protects", []) if str(hero or "").strip())

    rows = []
    seen = set()
    for raw_hero in candidate_heroes or []:
        hero = str(raw_hero or "").strip()
        hero_key = hero.lower()
        if not hero or hero_key in seen:
            continue
        seen.add(hero_key)

        profile = get_hero_profile(hero) or {}
        scores = get_hero_score(hero) or {}
        role = str(profile.get("role") or "").strip() or _machine_hero_role(hero)
        protect_priority = str(profile.get("ban_priority") or "medium").strip().lower()
        strategic_score = float(scores.get("strategic_contribution") or 5)
        stability = float(scores.get("stability") or 5)
        engage = float(scores.get("engage_score") or 5)
        poke = float(scores.get("poke_score") or 5)
        peel = float(scores.get("peel_score") or 5)
        sustain = float(scores.get("sustain_score") or 5)
        execution_burden = float(scores.get("execution_burden") or scores.get("execution_difficulty") or 5)
        pressure_type = str(scores.get("pressure_type") or profile.get("archetype") or "").strip()

        our_row = our_pool.get(hero_key) or our_comfort.get(hero_key) or {}
        enemy_row = enemy_pool.get(hero_key) or enemy_comfort.get(hero_key) or {}
        our_dependency = (
            float(our_row.get("comfort_rate", 0) or 0) * 0.56
            + float(our_row.get("adjusted_win_rate", 0) or our_row.get("raw_win_rate", 0) or 0) * 0.24
            + float(our_row.get("profile_score", 0) or 0) * 0.20
        )
        enemy_dependency = (
            float(enemy_row.get("comfort_rate", 0) or 0) * 0.56
            + float(enemy_row.get("adjusted_win_rate", 0) or enemy_row.get("raw_win_rate", 0) or 0) * 0.24
            + float(enemy_row.get("profile_score", 0) or 0) * 0.20
        )

        route_value = 0.0
        if hero_key in target_comp_keys:
            route_value += 28.0
        if hero_key in route_protects:
            route_value += 18.0
        enemy_ban_pressure = 18.0 if hero_key in enemy_expected_ban_keys else 0.0

        volatile = volatility.get(hero_key) or {}
        our_stat_impact = max(0.0, float(volatile.get("team_a_delta", 0) or 0))
        enemy_stat_cost = max(0.0, float(volatile.get("team_b_delta", 0) or 0))

        hero_pool_value = (
            strategic_score * 5.8
            + max(stability, peel, sustain, engage, poke) * 2.0
            + priority_value.get(protect_priority, 8.0)
            + max(0.0, 8.0 - execution_burden) * 1.1
        )
        strategic_our_value = our_dependency + route_value + enemy_ban_pressure + our_stat_impact + hero_pool_value * 0.28
        strategic_enemy_value = enemy_dependency + enemy_stat_cost + (hero_pool_value * 0.10 if enemy_comfort.get(hero_key) else 0.0)
        strategic_net_value = strategic_our_value - strategic_enemy_value
        protect_score = (
            strategic_our_value
            + hero_pool_value * 0.34
            - strategic_enemy_value * 0.48
            + max(0.0, stability - 6.0) * 1.6
        )

        strategic_caution = ""
        why_not_reason = ""
        if hero_key not in target_comp_keys and hero_key not in route_protects and our_dependency < 18:
            strategic_caution = "thin route value"
            why_not_reason = "does not clearly preserve the projected comp path"
        elif enemy_dependency > our_dependency + 10:
            strategic_caution = "shared/enemy value"
            why_not_reason = "protecting it may preserve a hero the enemy also values"

        if protect_score >= 76 and strategic_net_value > 18:
            model_hint = "excellent route preserve"
        elif strategic_net_value > 8:
            model_hint = "good protect candidate"
        elif strategic_net_value > -4:
            model_hint = "situational protect"
        else:
            model_hint = "low-value protect"

        rows.append(
            {
                "hero": hero,
                "role": role,
                "role_alias": role_alias.get(role, role),
                "archetype": profile.get("archetype", ""),
                "pressure_type": pressure_type,
                "protect_priority": protect_priority,
                "strategic_net_value": round(strategic_net_value, 1),
                "strategic_our_value": round(strategic_our_value, 1),
                "strategic_enemy_value": round(strategic_enemy_value, 1),
                "protect_score": round(protect_score, 1),
                "our_dependency": round(our_dependency, 1),
                "enemy_dependency": round(enemy_dependency, 1),
                "our_stat_impact": round(our_stat_impact, 1),
                "enemy_stat_cost": round(enemy_stat_cost, 1),
                "route_value": round(route_value, 1),
                "enemy_ban_pressure": round(enemy_ban_pressure, 1),
                "hero_pool_value": round(hero_pool_value, 1),
                "strategic_caution": strategic_caution,
                "why_not_reason": why_not_reason,
                "model_hint": model_hint,
            }
        )

    rows.sort(key=lambda row: (float(row.get("protect_score", 0) or 0), float(row.get("strategic_net_value", 0) or 0)), reverse=True)
    return rows


def _machine_agent_parse_slot_compare(message: str) -> dict | None:
    q = (message or "").strip().lower()
    if not q or not any(word in q for word in ("ban", "banning", "third", "fourth", "3rd", "4th", "difference", "compare")):
        return None

    hero_name = _machine_agent_parse_hero(message)
    if not hero_name:
        return None

    ordinal_slots = {
        "first": 1,
        "1st": 1,
        "one": 1,
        "second": 2,
        "2nd": 2,
        "two": 2,
        "third": 3,
        "3rd": 3,
        "three": 3,
        "fourth": 4,
        "4th": 4,
        "four": 4,
    }
    slots = []
    for word, number in ordinal_slots.items():
        if re.search(rf"\b{re.escape(word)}\b", q) and number not in slots:
            slots.append(number)
    for match in re.finditer(r"\bban\s*([1-4])\b", q):
        number = int(match.group(1))
        if number not in slots:
            slots.append(number)
    if len(slots) < 2:
        if 3 in slots:
            slots.append(4)
        elif 4 in slots:
            slots.insert(0, 3)
        else:
            return None
    return {"hero": hero_name, "slots": slots[:2]}


def _machine_agent_parse_hero(message: str) -> str:
    q = (message or "").strip().lower()
    for hero in sorted(HEROES, key=len, reverse=True):
        hero_key = hero.lower()
        if re.search(rf"(?<![a-z0-9]){re.escape(hero_key)}(?![a-z0-9])", q):
            return hero
    for alias, canonical in sorted(HERO_NAME_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        alias_text = alias.lower()
        if alias_text and re.search(rf"\b{re.escape(alias_text)}\b", q):
            return canonical
    return ""


def _machine_chat_filter_team_history(
    team_row,
    season_value: str,
    selected_map_name: str,
    include_scrims: bool,
    include_tournaments: bool,
    selected_map_type: str = "all",
    selected_mode_type: str = "all",
) -> list[dict]:
    def _normalize_mode_type(raw_value: str) -> str:
        value = str(raw_value or "").strip().lower()
        return value if value in {"control", "escort", "hybrid"} else "all"

    def _filter_scrims_by_mode_type(scrims: list[dict], mode_type_value: str) -> list[dict]:
        selected_mode = _normalize_mode_type(mode_type_value)
        if selected_mode == "all":
            return scrims
        mode_lookup = {
            str(map_name or "").strip().lower(): str(mode_name or "").strip().lower()
            for map_name, mode_name in (MAP_MODES or {}).items()
        }
        filtered_scrims: list[dict] = []
        for scrim in scrims or []:
            if not isinstance(scrim, dict):
                continue
            filtered_maps = []
            for map_entry in scrim.get("maps", []) or []:
                if not isinstance(map_entry, dict):
                    continue
                map_mode = str(map_entry.get("mode") or map_entry.get("map_mode") or "").strip().lower()
                if not map_mode:
                    map_name_key = str(map_entry.get("map") or map_entry.get("map_name") or "").strip().lower()
                    map_mode = mode_lookup.get(map_name_key, "")
                if map_mode == selected_mode:
                    filtered_maps.append(map_entry)
            if filtered_maps:
                scrim_copy = dict(scrim)
                scrim_copy["maps"] = filtered_maps
                filtered_scrims.append(scrim_copy)
        return filtered_scrims

    history = get_team_history_for_sources(
        team_row,
        include_scrims=include_scrims,
        include_tournaments=include_tournaments,
    )
    season_options = get_scrim_season_options(history)
    default_season = get_current_season_from_recent_scrim(history)
    has_unseasoned = any(not normalize_season_value(s.get("season", "")) for s in history)
    effective_default = "all" if not season_value or season_value.lower() == "all" else default_season
    selected_season = get_selected_season(
        season_value,
        season_options,
        allow_unspecified=has_unseasoned,
        default_season=effective_default,
        strict=True,
    )
    filtered = filter_scrims_by_season(history, selected_season)
    filtered = filter_scrims_by_map_type(filtered, get_selected_map_type(selected_map_type or "all"))
    filtered = _filter_scrims_by_mode_type(filtered, selected_mode_type)
    if selected_map_name and selected_map_name.lower() != "all":
        map_filtered = []
        for scrim in filtered:
            scrim_copy = dict(scrim)
            scrim_copy["maps"] = [
                m for m in scrim.get("maps", [])
                if ((m.get("map") or m.get("map_name") or "").strip().lower() == selected_map_name.lower())
            ]
            if scrim_copy["maps"]:
                map_filtered.append(scrim_copy)
        filtered = map_filtered
    return filtered


def _machine_agent_slot_timing_stats(history: list[dict], team_name: str, hero_name: str, slot_numbers: list[int]) -> dict:
    slot_keys = [f"ban{number}" for number in slot_numbers]
    rows = {
        slot_key: {
            "slot": slot_key,
            "label": f"Ban {slot_key[-1]}",
            "hero": hero_name,
            "maps": 0,
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "other_bans": Counter(),
            "protects": Counter(),
            "maps_by_name": Counter(),
        }
        for slot_key in slot_keys
    }
    total_complete_maps = 0

    for record in history:
        for map_entry in record.get("maps", []):
            if not isinstance(map_entry, dict):
                continue
            our_team_slot = map_entry.get("our_team_slot", "team1")
            if our_team_slot not in TEAM_SLOTS:
                our_team_slot = "team1"
            draft = map_entry.get("draft", {})
            our_draft = draft.get(our_team_slot, {}) if isinstance(draft, dict) else {}
            if not isinstance(our_draft, dict):
                continue
            if not any(_canonical_draft_hero(our_draft.get(slot_key, "")) for slot_key in DRAFT_SLOT_ORDER):
                continue
            total_complete_maps += 1
            outcome = get_map_outcome_for_slot(map_entry, our_team_slot)
            map_name = (map_entry.get("map_name") or map_entry.get("map") or "").strip()
            for slot_key in slot_keys:
                if _canonical_draft_hero(our_draft.get(slot_key, "")) != hero_name:
                    continue
                row = rows[slot_key]
                row["maps"] += 1
                if outcome == "Win":
                    row["wins"] += 1
                elif outcome == "Loss":
                    row["losses"] += 1
                else:
                    row["ties"] += 1
                if map_name:
                    row["maps_by_name"][map_name] += 1
                for other_slot in ("ban1", "ban2", "ban3", "ban4"):
                    other_hero = _canonical_draft_hero(our_draft.get(other_slot, ""))
                    if other_slot != slot_key and other_hero:
                        row["other_bans"][other_hero] += 1
                for protect_slot in ("protect1", "protect2"):
                    protect_hero = _canonical_draft_hero(our_draft.get(protect_slot, ""))
                    if protect_hero:
                        row["protects"][protect_hero] += 1

    formatted = []
    for slot_key in slot_keys:
        row = rows[slot_key]
        maps = int(row["maps"])
        wins = int(row["wins"])
        losses = int(row["losses"])
        formatted.append(
            {
                "slot": slot_key,
                "label": row["label"],
                "hero": hero_name,
                "maps": maps,
                "wins": wins,
                "losses": losses,
                "ties": int(row["ties"]),
                "rate": round((maps / total_complete_maps) * 100, 1) if total_complete_maps else 0.0,
                "win_rate": round((wins / (wins + losses)) * 100, 1) if (wins + losses) else 0.0,
                "common_other_bans": [hero for hero, _count in row["other_bans"].most_common(4)],
                "common_protects": [hero for hero, _count in row["protects"].most_common(3)],
                "common_maps": [map_name for map_name, _count in row["maps_by_name"].most_common(3)],
            }
        )

    leader = None
    if len(formatted) >= 2:
        left, right = formatted[0], formatted[1]
        if left["maps"] and right["maps"]:
            wr_delta = round(left["win_rate"] - right["win_rate"], 1)
            if abs(wr_delta) >= 0.1:
                leader = left["label"] if wr_delta > 0 else right["label"]
        elif left["maps"] or right["maps"]:
            leader = left["label"] if left["maps"] > right["maps"] else right["label"]

    return {
        "team": team_name,
        "hero": hero_name,
        "total_draft_maps": total_complete_maps,
        "slots": formatted,
        "leader": leader,
    }


def _machine_agent_opposite_slot(team_slot: str) -> str:
    return "team2" if team_slot == "team1" else "team1"


def _machine_agent_player_names_for_team(team_id: int | None) -> list[str]:
    if not team_id:
        return []
    rows = get_db().execute(
        """
        SELECT name FROM players WHERE team_id = ?
        UNION
        SELECT name FROM enemy_players WHERE enemy_team_id = ?
        ORDER BY name COLLATE NOCASE
        """,
        (team_id, team_id),
    ).fetchall()
    return [(row["name"] or "").strip() for row in rows if (row["name"] or "").strip()]


def _machine_agent_find_player_name(message: str, team_ids: list[int | None]) -> str:
    text = (message or "").lower()
    candidates = []
    for team_id in team_ids:
        for name in _machine_agent_player_names_for_team(team_id):
            key = name.lower()
            if key and key in text:
                candidates.append((len(key), name))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]

    history_names = set()
    for team_id in team_ids:
        if not team_id:
            continue
        row = get_db().execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
        if row is None:
            continue
        for record in get_team_history_for_sources(row, include_scrims=True, include_tournaments=True):
            for map_entry in record.get("maps", []):
                for section in map_entry.get("comp", []):
                    if not isinstance(section, dict):
                        continue
                    for side in TEAM_SLOTS:
                        for slot in section.get(side, []):
                            if isinstance(slot, dict):
                                player = (slot.get("player") or "").strip()
                                if player:
                                    history_names.add(player)
    for name in sorted(history_names, key=len, reverse=True):
        if name.lower() in text:
            return name
    return ""


def _machine_agent_parse_player_pivot(message: str, context: dict) -> dict | None:
    q = (message or "").lower()
    if not any(word in q for word in ("pivot", "swap", "switch", "go to", "play if", "likely to")):
        return None
    if not any(word in q for word in ("ban", "banned", "deny", "remove", "if i ban", "if we ban")):
        return None
    hero = _machine_agent_parse_hero(message)
    if not hero:
        return None
    team_ids = [context.get("team_b_id"), context.get("team_a_id")]
    player = _machine_agent_find_player_name(message, team_ids)
    if not player:
        return None
    target_team_id = None
    for team_id in team_ids:
        if player in _machine_agent_player_names_for_team(team_id):
            target_team_id = team_id
            break
    return {
        "player": player,
        "banned_hero": hero,
        "target_team_id": target_team_id or context.get("team_b_id") or context.get("team_a_id"),
    }


def _machine_agent_player_pivot_missing_response(message: str, context: dict) -> str:
    hero = _machine_agent_parse_hero(message)
    player = _machine_agent_find_player_name(message, [context.get("team_b_id"), context.get("team_a_id")])
    if not player and not hero:
        return "Which player and banned hero should I check? Example: `Who does Polly pivot to if we ban Captain America?`"
    if not player:
        return f"Which player should I check for the {hero} ban?"
    return f"Which banned hero should I use for {player}'s pivot read?"


def _machine_agent_player_pivot_stats(
    history: list[dict],
    player_name: str,
    banned_hero: str,
) -> dict:
    player_key = (player_name or "").strip().lower()
    banned_key = (banned_hero or "").strip().lower()
    banned_hero = _canonical_draft_hero(banned_hero)
    pivot_counts: Counter[str] = Counter()
    pivot_wins: Counter[str] = Counter()
    normal_counts: Counter[str] = Counter()
    normal_wins: Counter[str] = Counter()
    banned_maps = 0
    player_banned_maps = 0
    total_player_maps = 0

    for record in history:
        for map_entry in record.get("maps", []):
            if not isinstance(map_entry, dict):
                continue
            team_slot = map_entry.get("our_team_slot", "team1")
            if team_slot not in TEAM_SLOTS:
                team_slot = "team1"
            opponent_slot = _machine_agent_opposite_slot(team_slot)
            draft = map_entry.get("draft", {})
            opponent_draft = draft.get(opponent_slot, {}) if isinstance(draft, dict) else {}
            if not isinstance(opponent_draft, dict):
                opponent_draft = {}
            opponent_bans = {
                _canonical_draft_hero(opponent_draft.get(slot_key, ""))
                for slot_key in ("ban1", "ban2", "ban3", "ban4")
            }
            hero_was_banned = banned_hero in opponent_bans
            if hero_was_banned:
                banned_maps += 1

            heroes_this_map = set()
            for section in map_entry.get("comp", []):
                if not isinstance(section, dict):
                    continue
                for slot in section.get(team_slot, []):
                    if not isinstance(slot, dict):
                        continue
                    if (slot.get("player", "") or "").strip().lower() != player_key:
                        continue
                    hero = _canonical_draft_hero(slot.get("hero", ""))
                    if hero:
                        heroes_this_map.add(hero)

            if not heroes_this_map:
                continue

            total_player_maps += 1
            result = get_map_outcome_for_slot(map_entry, team_slot)
            for hero in heroes_this_map:
                normal_counts[hero] += 1
                if result == "Win":
                    normal_wins[hero] += 1
            if hero_was_banned:
                player_banned_maps += 1
                for hero in heroes_this_map:
                    pivot_counts[hero] += 1
                    if result == "Win":
                        pivot_wins[hero] += 1

    def rows_from_counts(counts: Counter[str], wins: Counter[str], total: int, limit: int = 5) -> list[dict]:
        rows = []
        for hero, count in counts.most_common(limit):
            rows.append({
                "hero": hero,
                "count": int(count),
                "rate": round((count / total) * 100, 1) if total else 0.0,
                "win_rate": round((wins[hero] / count) * 100, 1) if count else 0.0,
            })
        return rows

    return {
        "player": player_name,
        "banned_hero": banned_hero,
        "banned_maps": banned_maps,
        "player_banned_maps": player_banned_maps,
        "total_player_maps": total_player_maps,
        "pivots": rows_from_counts(pivot_counts, pivot_wins, player_banned_maps),
        "fallback": rows_from_counts(normal_counts, normal_wins, total_player_maps),
    }


def _machine_agent_player_pivot_answer(stats: dict, team_name: str) -> str:
    pivots = stats.get("pivots", [])
    fallback = stats.get("fallback", [])
    player = stats.get("player", "That player")
    banned_hero = stats.get("banned_hero", "that hero")
    if pivots:
        top = pivots[0]
        return (
            f"If you ban {banned_hero}, I expect {player} to land on {top['hero']}.\n\n"
            f"Sample: {top['count']} of {stats.get('player_banned_maps', 0)} matching maps | {top['rate']}% pivot rate | {top['win_rate']}% WR."
        )
    if fallback:
        top = fallback[0]
        return (
            f"I do not have a clean ban-triggered sample for {player} yet.\n\n"
            f"Fallback: {top['hero']} | {top['count']} of {stats.get('total_player_maps', 0)} player maps | {top['rate']}% pick rate | {top['win_rate']}% WR."
        )
    return (
        f"I do not have a usable pivot sample for {player} in these filters.\n\n"
        "Try a wider season or all maps."
    )


def _resolve_team_by_name(name: str) -> int | None:
    """Resolve a team name to its DB id. Tries exact match then partial."""
    if not name:
        return None
    db = get_db()
    row = db.execute("SELECT id FROM teams WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row["id"]
    row = db.execute("SELECT id FROM teams WHERE name LIKE ? COLLATE NOCASE LIMIT 1", (f"%{name}%",)).fetchone()
    return row["id"] if row else None


def _machine_chat_build_context(
    team_a_id: int | None,
    team_b_id: int | None,
    season_value: str,
    selected_map_name: str,
    include_scrims: bool,
    include_tournaments: bool,
    selected_map_type: str = "all",
    selected_mode_type: str = "all",
) -> tuple[str, dict]:
    db = get_db()
    source_label = []
    if include_scrims:
        source_label.append("scrims")
    if include_tournaments:
        source_label.append("tournaments")

    if not team_a_id or not team_b_id:
        teams = db.execute("SELECT name FROM teams ORDER BY name COLLATE NOCASE LIMIT 18").fetchall()
        return (
            "No matchup selected. Available teams include: "
            + _machine_chat_join([row["name"] for row in teams], 18)
            + ". Ask the user to pick both teams for matchup-specific draft advice.",
            {"has_matchup": False},
        )

    team_rows = db.execute(
        "SELECT * FROM teams WHERE id IN (?, ?)", (team_a_id, team_b_id)
    ).fetchall()
    team_lookup = {row["id"]: row for row in team_rows}
    team_a = team_lookup.get(team_a_id)
    team_b = team_lookup.get(team_b_id)
    if team_a is None or team_b is None:
        abort(404)

    a_history = _machine_chat_filter_team_history(
        team_a,
        season_value,
        selected_map_name,
        include_scrims,
        include_tournaments,
        selected_map_type,
        selected_mode_type,
    )
    b_history = _machine_chat_filter_team_history(
        team_b,
        season_value,
        selected_map_name,
        include_scrims,
        include_tournaments,
        selected_map_type,
        selected_mode_type,
    )
    model = build_matchup_tree_model(team_a["name"], a_history, team_b["name"], b_history)
    team_models = model.get("teams", [])
    a_model = (team_models[0].get("model", {}) if len(team_models) > 0 else {})
    b_model = (team_models[1].get("model", {}) if len(team_models) > 1 else {})

    def comfort_rows(team_model):
        return _machine_chat_row_list(
            team_model.get("comfort_core_rows", []),
            lambda r: f"{r.get('hero')} ({r.get('comfort_rate', 0)}% comfort, {r.get('adjusted_win_rate', r.get('win_rate', 0))}% adj WR)",
            8,
        )

    def line_rows(team_model, key):
        return _machine_chat_row_list(
            team_model.get(key, []),
            lambda r: f"{_machine_chat_join(r.get('heroes', []), 6)} ({r.get('rate', 0)}% rate, {r.get('line_win_rate', r.get('comp_win_rate', 0))}% WR)",
            4,
        )

    recommended_bans = []
    enemy_expected_bans = []
    enemy_expected_protects = []
    recommended_protects = []
    target_comp = []

    def add_unique(target: list[str], values: list[str], limit: int) -> None:
        existing = {v.lower() for v in target}
        for value in values:
            hero = (value or "").strip()
            key = hero.lower()
            if hero and key not in existing:
                target.append(hero)
                existing.add(key)
            if len(target) >= limit:
                break

    def _build_slot_history_from_lines(line_rows: list[dict], kind: str) -> dict[str, dict[str, float]]:
        slot_keys = ["protect1", "protect2"] if kind == "protect" else ["ban1", "ban2", "ban3", "ban4"]
        lookup: dict[str, dict[str, float]] = {slot: {} for slot in slot_keys}
        for row in line_rows or []:
            count = float(row.get("count", 0) or 0)
            rate = float(row.get("rate", 0) or 0)
            line_strength = float(row.get("line_strength", 0) or 0)
            sequence = (row.get("protects") if kind == "protect" else (row.get("bans") or row.get("heroes"))) or []
            for idx, hero in enumerate(sequence):
                if idx >= len(slot_keys):
                    break
                canonical = _canonical_draft_hero(str(hero or "").strip())
                if not canonical:
                    continue
                hero_key = canonical.lower()
                slot_key = slot_keys[idx]
                score = count + (rate * 0.35) + (line_strength * 0.18)
                current = float(lookup[slot_key].get(hero_key, 0.0) or 0.0)
                lookup[slot_key][hero_key] = round(current + score, 3)
        return lookup

    def _slot_ranked_history(
        slot_history_lookup: dict[str, dict[str, float]],
        slot_key: str,
        seed_values: list[str],
        blocked_values: list[str],
        limit: int,
    ) -> list[str]:
        ranked: list[str] = []
        blocked_keys = {
            _canonical_draft_hero(str(hero or "").strip()).lower()
            for hero in blocked_values or []
            if str(hero or "").strip()
        }
        slot_scores = slot_history_lookup.get(slot_key) or {}
        historical_sorted = sorted(
            slot_scores.items(),
            key=lambda item: (float(item[1] or 0), item[0]),
            reverse=True,
        )
        add_unique(
            ranked,
            [hero_key for hero_key, _score in historical_sorted if hero_key not in blocked_keys],
            limit,
        )
        add_unique(ranked, seed_values, limit)
        return [_canonical_draft_hero(hero) for hero in ranked if _canonical_draft_hero(hero)]

    def _historical_ban_pool(line_rows: list[dict], limit: int = 12) -> list[str]:
        pool: list[str] = []
        for row in line_rows or []:
            add_unique(pool, [str(hero or "").strip() for hero in (row.get("bans") or row.get("heroes") or [])], limit)
            if len(pool) >= limit:
                break
        return pool

    has_strong_comp_data = float(a_model.get("training_maps", 0) or 0) >= 20
    comp_usage_floor = 8.0 if has_strong_comp_data else 4.0
    comp_win_rate_floor = max(45.0, float(a_model.get("overall_win_rate", 0) or 0) - 6.0)

    def _is_viable_target_comp_row(row: dict | None) -> bool:
        row = row or {}
        usage = float(row.get("rate", 0) or 0)
        sample_count = float(row.get("count", 0) or 0)
        comp_wr = float(row.get("comp_win_rate", row.get("win_rate", 0)) or 0)
        adjusted = float(row.get("avg_adjusted_win_rate", 0) or 0)
        blended_wr = (comp_wr * 0.75) + (adjusted * 0.25)
        if has_strong_comp_data and sample_count < 3:
            return False
        if has_strong_comp_data and usage < comp_usage_floor:
            return False
        if sample_count >= 3 and blended_wr > 0 and blended_wr < comp_win_rate_floor:
            return False
        return True

    historical_enemy_bans = _historical_ban_pool(b_model.get("ban_line_rows", []), 8)

    add_unique(recommended_bans, historical_enemy_bans, 6)
    for row in model.get("force_matchup_rows", [])[:3]:
        add_unique(recommended_protects, row.get("our_protects", []), 3)
        if not target_comp:
            add_unique(target_comp, row.get("our_comp", []), 6)
    for row in b_model.get("ban_line_rows", [])[:5]:
        add_unique(enemy_expected_bans, row.get("bans", []), 8)
        add_unique(enemy_expected_protects, row.get("protects", []), 6)
    if not target_comp:
        comp_rows = a_model.get("comp_rows") or []
        viable_comp_row = next((row for row in comp_rows if _is_viable_target_comp_row(row)), None)
        comp_row = viable_comp_row or (comp_rows[0] if comp_rows else {})
        add_unique(target_comp, comp_row.get("heroes", []), 6)

    our_ban_slot_history = _build_slot_history_from_lines(a_model.get("ban_line_rows", []), "ban")
    our_protect_slot_history = _build_slot_history_from_lines(a_model.get("ban_line_rows", []), "protect")
    enemy_ban_slot_history = _build_slot_history_from_lines(b_model.get("ban_line_rows", []), "ban")
    enemy_protect_slot_history = _build_slot_history_from_lines(b_model.get("ban_line_rows", []), "protect")
    enemy_expected_bans = _slot_ranked_history(
        enemy_ban_slot_history,
        "ban1",
        enemy_expected_bans,
        [],
        8,
    )
    enemy_expected_protects = _slot_ranked_history(
        enemy_protect_slot_history,
        "protect1",
        enemy_expected_protects,
        [],
        6,
    )

    enemy_comps = [
        {
            "heroes": row.get("heroes", [])[:6],
            "rate": row.get("rate", 0),
            "win_rate": row.get("comp_win_rate", row.get("win_rate", 0)),
            "confidence": row.get("ml_confidence", 0),
        }
        for row in b_model.get("comp_rows", [])[:4]
    ]
    ban_candidate_pool = []
    add_unique(ban_candidate_pool, recommended_bans, 10)
    add_unique(ban_candidate_pool, historical_enemy_bans, 10)
    ban_anova_significance = _machine_build_ban_anova_rows(b_history, ban_candidate_pool)
    ban_permutation_importance = _machine_build_ban_permutation_importance(b_history, ban_candidate_pool)
    ban_candidate_details = _machine_build_ban_candidate_details(
        ban_candidate_pool,
        a_model=a_model,
        b_model=b_model,
        matchup_model=model,
        target_comp=target_comp,
        enemy_comps=enemy_comps,
        ban_significance=ban_anova_significance,
        permutation_importance=ban_permutation_importance,
    )
    if ban_candidate_details:
        for row in ban_candidate_details:
            hero_name = _canonical_draft_hero(str(row.get("hero") or "").strip())
            if not hero_name:
                continue
            slot_stats = _machine_agent_slot_timing_stats(a_history, team_a["name"], hero_name, [1, 2, 3, 4])
            slot_lookup = {
                str(slot_row.get("slot") or "").strip().lower(): {
                    "maps": int(slot_row.get("maps", 0) or 0),
                    "rate": float(slot_row.get("rate", 0) or 0),
                    "win_rate": float(slot_row.get("win_rate", 0) or 0),
                }
                for slot_row in (slot_stats.get("slots") or [])
                if str(slot_row.get("slot") or "").strip()
            }
            row["historical_slot_bans"] = slot_lookup
    if ban_candidate_details:
        historical_first_bans = list(recommended_bans)
        model_ranked_bans = [row["hero"] for row in ban_candidate_details if (row.get("hero") or "").strip()]
        if historical_first_bans:
            merged_bans = list(historical_first_bans)
            add_unique(merged_bans, model_ranked_bans, 6)
            recommended_bans = merged_bans[:6]
        else:
            recommended_bans = model_ranked_bans[:6]
    if not recommended_bans:
        recommended_bans = historical_enemy_bans[:6]
    protect_candidate_pool = []
    add_unique(protect_candidate_pool, recommended_protects, 10)
    add_unique(protect_candidate_pool, target_comp, 10)
    add_unique(protect_candidate_pool, [r.get("hero", "") for r in a_model.get("comfort_core_rows", [])], 10)
    add_unique(protect_candidate_pool, [r.get("hero", "") for r in model.get("contested_heroes", [])], 10)
    protect_candidate_details = _machine_build_protect_candidate_details(
        protect_candidate_pool,
        a_model=a_model,
        b_model=b_model,
        matchup_model=model,
        target_comp=target_comp,
        enemy_expected_bans=enemy_expected_bans,
    )
    if protect_candidate_details:
        historical_first_protects = list(recommended_protects)
        model_ranked_protects = [row["hero"] for row in protect_candidate_details if (row.get("hero") or "").strip()]
        if historical_first_protects:
            merged_protects = list(historical_first_protects)
            add_unique(merged_protects, model_ranked_protects, 3)
            recommended_protects = merged_protects[:3]
        else:
            recommended_protects = model_ranked_protects[:3]
    pivot_predictions = [
        {
            "base": row.get("enemy_base", row.get("base_heroes", []))[:6],
            "pivot": row.get("enemy_pivot", [])[:6],
            "counter": row.get("our_counter_pivot", [])[:6],
            "diff_count": row.get("enemy_diff_count", 0),
            "counter_diff_count": row.get("our_counter_diff_count", 0),
        }
        for row in model.get("pivot_pressure_rows", [])[:4]
    ]

    top_enemy_comp = enemy_comps[0] if enemy_comps else {}
    top_pivot = pivot_predictions[0] if pivot_predictions else {}
    top_force = (model.get("force_matchup_rows") or [{}])[0]
    coach_intent = "preserve their highest-comfort comp route"
    if top_enemy_comp.get("heroes"):
        coach_intent = f"preserve {_machine_chat_join(top_enemy_comp.get('heroes', []), 4)} as the main comp shell"
    elif top_pivot.get("base"):
        coach_intent = f"start on {_machine_chat_join(top_pivot.get('base', []), 4)} and keep the pivot open"

    draft_tree = {
        "root": {
            "title": "Opponent coach read",
            "subtitle": f"Likely trying to {coach_intent}.",
        },
        "branches": [
            {
                "label": "Primary line",
                "thought": coach_intent,
                "bans": recommended_bans[:3],
                "protects": [
                    hero for hero in (top_enemy_comp.get("heroes", []) or [])[:2]
                    if hero not in recommended_bans[:3]
                ],
                "outcome": "If unbroken, they keep their preferred fight shape and force us to answer on comp quality.",
            },
            {
                "label": "Our break point",
                "thought": "Remove the piece that collapses the route instead of only banning a name.",
                "bans": recommended_bans[:2],
                "protects": recommended_protects[:2],
                "outcome": (
                    f"Best interruption is {_machine_chat_join(recommended_bans[:1], 1)}."
                    if recommended_bans else "No single clean interruption yet."
                ),
            },
            {
                "label": "Expected pivot",
                "thought": "If the primary route is denied, they try to preserve identity through a pivot.",
                "base": top_pivot.get("base", [])[:6],
                "pivot": top_pivot.get("pivot", [])[:6],
                "counter": top_pivot.get("counter", [])[:6],
                "outcome": (
                    f"{top_pivot.get('diff_count', 0)} hero shift."
                    if top_pivot else "No strong pivot sample yet."
                ),
            },
        ],
    }

    comp_tree = {
        "root": {
            "title": "Expected comp outcomes",
            "subtitle": "Most likely enemy shell, pivot shell, and our answer.",
        },
        "outcomes": [
            {
                "label": "Primary enemy comp",
                "heroes": top_enemy_comp.get("heroes", [])[:6],
                "rate": top_enemy_comp.get("rate", 0),
                "win_rate": top_enemy_comp.get("win_rate", 0),
                "why": "Default outcome if their coach preserves the current route.",
            },
            {
                "label": "Pivot outcome",
                "heroes": top_pivot.get("pivot", [])[:6],
                "trigger": (
                    "Triggered when we remove " + _machine_chat_join(recommended_bans[:1], 1)
                    if recommended_bans else "Triggered if their primary shell is denied."
                ),
                "why": "Keeps their identity alive if the base route gets broken.",
            },
            {
                "label": "Our answer",
                "heroes": (top_pivot.get("counter", []) or top_force.get("our_comp", []) or target_comp)[:6],
                "why": "Counter-pivot or target comp path into their expected outcome.",
            },
        ],
    }
    comp_confidence = 0
    comp_wr = 0
    for row in a_model.get("comp_rows", []):
        if set(target_comp) == set(row.get("heroes", [])):
            comp_confidence = row.get("ml_confidence", 0)
            comp_wr = row.get("ml_win_prob", row.get("comp_win_rate", 0))
            break

    context_lines = [
        f"Matchup: {team_a['name']} vs {team_b['name']}.",
        f"Filters: season={season_value or 'all'}, map={selected_map_name or 'all'}, map_type={selected_map_type or 'all'}, mode_type={selected_mode_type or 'all'}, sources={_machine_chat_join(source_label) or 'none'}.",
        f"Data volume: {team_a['name']} {len(a_history)} records, {team_b['name']} {len(b_history)} records.",
        "Model method map: "
        + "; ".join(f"{task}={method}" for task, method in MACHINE_DRAFT_METHOD_MAP.items())
        + ".",
        f"Recommended ban targets: {_machine_chat_join(recommended_bans, 6)}.",
        "Ban candidate detail: "
        + _machine_chat_row_list(
            ban_candidate_details,
            lambda r: (
                f"{r.get('hero')} ({r.get('role')}, score {r.get('ban_score')}, "
                f"net {r.get('strategic_net_value')}, enemy {r.get('strategic_enemy_value')}, "
                f"our cost {r.get('strategic_our_value')}, "
                f"ANOVA {r.get('anova_significance', {}).get('significance', 'n/a')} "
                f"F={r.get('anova_significance', {}).get('f_stat', 0)}, "
                f"lift={r.get('anova_significance', {}).get('loss_lift_pp', 0)}pp, "
                f"perm={r.get('permutation_importance', {}).get('importance', 'n/a')} "
                f"rel={r.get('permutation_importance', {}).get('relative_lift', 0)}, "
                f"hint {r.get('model_hint')})"
            ),
            6,
        )
        + ".",
        f"Recommended protects: {_machine_chat_join(recommended_protects, 3)}.",
        "Protect candidate detail: "
        + _machine_chat_row_list(
            protect_candidate_details,
            lambda r: (
                f"{r.get('hero')} ({r.get('role')}, score {r.get('protect_score')}, "
                f"net {r.get('strategic_net_value')}, our value {r.get('strategic_our_value')}, "
                f"enemy value {r.get('strategic_enemy_value')}, hint {r.get('model_hint')})"
            ),
            6,
        )
        + ".",
        f"Target comp path: {_machine_chat_join(target_comp, 6)}.",
        f"{team_a['name']} comfort heroes: {comfort_rows(a_model)}.",
        f"{team_b['name']} comfort heroes: {comfort_rows(b_model)}.",
        f"{team_a['name']} common ban lines: {line_rows(a_model, 'ban_line_rows')}.",
        f"{team_b['name']} common ban lines: {line_rows(b_model, 'ban_line_rows')}.",
        f"{team_a['name']} common comps: {line_rows(a_model, 'comp_rows')}.",
        f"{team_b['name']} common comps: {line_rows(b_model, 'comp_rows')}.",
        "Contested heroes: "
        + _machine_chat_row_list(
            model.get("contested_heroes", []),
            lambda r: f"{r.get('hero')} ({team_a['name']} {r.get('team_a_comfort', 0)}%, {team_b['name']} {r.get('team_b_comfort', 0)}%)",
            8,
        )
        + ".",
        "Force paths: "
        + _machine_chat_row_list(
            model.get("force_matchup_rows", []),
            lambda r: f"{team_a['name']} comp {_machine_chat_join(r.get('our_comp', []))}; bans {_machine_chat_join(r.get('our_bans', []))}; expected enemy {_machine_chat_join(r.get('enemy_comp', []))}",
            4,
        )
        + ".",
        "Volatile heroes: "
        + _machine_chat_row_list(
            model.get("volatile_matchup_rows", []),
            lambda r: f"{r.get('hero')} favors {r.get('favored_side')} (delta {r.get('delta', 0)})",
            6,
        )
        + ".",
        "Map consensus: "
        + _machine_chat_row_list(
            model.get("map_consensus_rows", []),
            lambda r: f"{r.get('label')}: {_machine_chat_join([o.get('map_name') for o in r.get('options', [])], 4)}",
            4,
        )
        + ".",
        f"Opponent coach read: likely trying to {coach_intent}.",
        "Draft tree: "
        + "; ".join(
            f"{branch.get('label')}: {branch.get('thought')} -> {branch.get('outcome')}"
            for branch in draft_tree.get("branches", [])
        )
        + ".",
        "Comp tree outcomes: "
        + "; ".join(
            f"{outcome.get('label')}: {_machine_chat_join(outcome.get('heroes', []), 6)}"
            for outcome in comp_tree.get("outcomes", [])
        )
        + ".",
    ]
    return "\n".join(context_lines), {
        "has_matchup": True,
        "team_a": team_a["name"],
        "team_b": team_b["name"],
        "model_status": model.get("status", "ready"),
        "visuals": {
            "recommended_bans": recommended_bans[:6],
            "ban_candidate_details": ban_candidate_details[:8],
            "ban_anova_significance": ban_anova_significance,
            "ban_permutation_importance": ban_permutation_importance,
            "model_method_map": MACHINE_DRAFT_METHOD_MAP,
            "enemy_expected_bans": enemy_expected_bans[:6],
            "enemy_expected_protects": enemy_expected_protects[:6],
            "our_ban_slot_history": our_ban_slot_history,
            "our_protect_slot_history": our_protect_slot_history,
            "enemy_ban_slot_history": enemy_ban_slot_history,
            "enemy_protect_slot_history": enemy_protect_slot_history,
            "recommended_protects": recommended_protects[:3],
            "protect_candidate_details": protect_candidate_details[:8],
            "target_comp": target_comp[:6],
            "our_comfort": [
                row.get("hero", "")
                for row in a_model.get("comfort_core_rows", [])[:6]
                if (row.get("hero", "") or "").strip()
            ],
            "enemy_comfort": [
                row.get("hero", "")
                for row in b_model.get("comfort_core_rows", [])[:6]
                if (row.get("hero", "") or "").strip()
            ],
            "contested": [
                row.get("hero", "")
                for row in model.get("contested_heroes", [])[:6]
                if (row.get("hero", "") or "").strip()
            ],
            "volatile": [
                row.get("hero", "")
                for row in model.get("volatile_matchup_rows", [])[:6]
                if (row.get("hero", "") or "").strip()
            ],
            "volatile_rows": [
                {
                    "hero": row.get("hero", ""),
                    "favored_side": row.get("favored_side", ""),
                    "delta": row.get("delta", 0),
                }
                for row in model.get("volatile_matchup_rows", [])[:8]
                if (row.get("hero", "") or "").strip()
            ],
            "likely_next_pick": target_comp[:1] or [
                row.get("hero", "")
                for row in a_model.get("comfort_core_rows", [])[:1]
                if (row.get("hero", "") or "").strip()
            ],
            "enemy_comps": enemy_comps,
            "pivot_predictions": pivot_predictions,
            "coach_read": {
                "intent": coach_intent,
                "break_point": recommended_bans[:1],
                "expected_primary_comp": top_enemy_comp.get("heroes", [])[:6],
                "expected_pivot": top_pivot.get("pivot", [])[:6],
                "our_answer": (top_pivot.get("counter", []) or top_force.get("our_comp", []) or target_comp)[:6],
            },
            "draft_tree": draft_tree,
            "comp_tree": comp_tree,
            "our_comp_rows": [
                {
                    "heroes": row.get("heroes", [])[:6],
                    "rate": row.get("rate", 0),
                    "win_rate": row.get("comp_win_rate", row.get("line_win_rate", row.get("win_rate", 0))),
                }
                for row in a_model.get("comp_rows", [])[:5]
                if row.get("heroes")
            ],
            "confidence": {
                "target_comp": target_comp[:6],
                "confidence": round(float(comp_confidence or 0), 1),
                "win_rate": round(float(comp_wr or 0), 1),
                "sample": len(a_history),
            },
            "map_consensus": [
                {
                    "label": row.get("label", ""),
                    "maps": [
                        opt.get("map_name", "")
                        for opt in row.get("options", [])[:4]
                        if (opt.get("map_name", "") or "").strip()
                    ],
                }
                for row in model.get("map_consensus_rows", [])[:4]
            ],
        },
    }


def _machine_chat_local_answer(message: str, context_text: str, meta: dict) -> str:
    if not meta.get("has_matchup"):
        return "Pick both teams in the agent controls and I can produce a matchup-specific draft read."

    question = (message or "").lower()
    lines = context_text.splitlines()
    wanted = []
    if any(word in question for word in ("ban", "bans", "protect", "counter")):
        wanted = [
            line for line in lines
            if line.startswith("Recommended ban targets")
            or line.startswith("Recommended protects")
            or line.startswith("Target comp path")
            or "common ban lines" in line
            or line.startswith("Volatile heroes")
            or line.startswith("Contested heroes")
        ]
    elif any(word in question for word in ("map", "maps")):
        wanted = [line for line in lines if line.startswith("Map consensus") or line.startswith("Filters") or "Data volume" in line]
    elif any(word in question for word in ("comp", "composition", "play")):
        wanted = [line for line in lines if line.startswith("Target comp path") or line.startswith("Recommended protects") or "common comps" in line or "Force paths:" in line or "comfort heroes" in line]
    else:
        wanted = [line for line in lines if line.startswith("Matchup") or "comfort heroes" in line or line.startswith("Contested") or line.startswith("Force paths")]

    return "Draft agent read\n\n" + "\n".join(wanted[:6])


def _machine_agent_is_info_request(message: str, intent: str) -> bool:
    q = (message or "").lower()
    # Snapshot/profile style asks should always route through site-data lookup,
    # even if the intent parser classifies them as draft-oriented (e.g., comp snapshot).
    snapshot_phrases = (
        "snapshot",
        "profile",
        "overview",
        "breakdown",
        "summary",
        "tell me about",
        "show me",
        "give me",
        "what do you know about",
        "info on",
        "information on",
        "information about",
    )
    if any(phrase in q for phrase in snapshot_phrases):
        return True
    if "tournament" in q and any(token in q for token in ("comp", "composition", "player", "team", "hero", "map", "stats", "record", "history")):
        return True

    if intent in {
        "ban",
        "protect",
        "comp",
        "risk",
        "check",
        "next_pick",
        "enemy_comps",
        "ban_impact",
        "player_pivot",
        "pivot",
        "confidence",
        "contested",
        "comfort",
        "hero_volatility",
        "slot_compare",
    }:
        return False
    return any(
        phrase in q
        for phrase in (
            "who is",
            "what is",
            "what are",
            "what was",
            "show",
            "tell me",
            "give me",
            "list",
            "history",
            "record",
            "stats",
            "stat",
            "overview",
            "profile",
            "snapshot",
            "breakdown",
            "hero pool",
            "heroes does",
            "plays",
            "played",
            "how many",
            "recent scrim",
            "recent match",
            "our team",
            "my team",
            "our roster",
            "our players",
            "our comps",
            "our heroes",
            "our maps",
            "everything about",
            "all about",
            "info on",
            "summary",
        )
    )


def _machine_agent_site_answer(message: str, season_value: str | None = None) -> dict | None:
    from draft_engine.site_context import search_site

    season = None if not season_value or season_value == "all" else season_value
    result = search_site(get_db(), message, season)
    sources = result.get("sources_used", [])
    if not sources or sources == ["none — no matching entity found in site data"]:
        return None

    def hero_bits(rows: list[dict], limit: int = 6) -> str:
        values = []
        for row in (rows or [])[:limit]:
            hero = (row.get("hero") or "").strip()
            if not hero:
                continue
            appearances = row.get("appearances") or row.get("played_count") or row.get("count") or row.get("co_appearances") or 0
            has_record = any(key in row for key in ("wins", "losses", "played_wins", "played_losses"))
            wins = row.get("wins", row.get("played_wins", 0)) or 0
            losses = row.get("losses", row.get("played_losses", 0)) or 0
            if appearances and has_record and (wins + losses):
                wr = round((wins / (wins + losses)) * 100, 1)
                values.append(f"{hero} ({appearances}, {wr}% WR)")
            elif appearances:
                values.append(f"{hero} ({appearances})")
            else:
                values.append(hero)
        return _machine_chat_join(values, limit)

    q = (message or "").lower()
    wants_scrims = any(phrase in q for phrase in ("scrim", "scrims", "history", "recent match", "recent matches", "recent game", "results"))
    wants_comps = any(phrase in q for phrase in (
        "what comp", "what comps", "comps do", "comp do", "comps does", "comp does",
        "comps can", "comp can", "comps will", "comp will", "comps they", "comp they",
        "comp style", "comp options", "play in", "run in", "draft style",
    ))
    wants_comfort = wants_comps or any(phrase in q for phrase in (
        "hero pool",
        "comfort heroes",
        "main heroes",
        "signature heroes",
        "best heroes",
        "most played heroes",
    ))
    wants_player = any(phrase in q for phrase in ("who is", "who plays", "heroes does", "hero pool", "player"))
    wants_hero = any(phrase in q for phrase in (
        "tell me about", "banned", "protected", "how much does", "how often does",
        "how many times", "ban rate", "does ban",
    )) and not any(phrase in q for phrase in ("team profile", "team overview", "profile", "overview"))
    wants_team = wants_comps or any(phrase in q for phrase in ("overview", "profile", "snapshot", "breakdown", "everything about", "all about", "tell me about"))
    wants_map = any(phrase in q for phrase in ("map", "maps")) and any(
        phrase in q
        for phrase in (
            "record", "stats", "played", "good on", "best on", "best map", "strongest map", "favorite map", "go to map",
        )
    )
    wants_bans = any(phrase in q for phrase in ("ban", "bans", "banned", "ban rate", "ban stats"))

    section_map = {}

    # --- Player section ---
    players = result.get("players", []) or []
    if players:
        player = players[0]
        section_map["player"] = (
            f"{player.get('player_name') or 'That player'} is on {player.get('team_name') or 'the roster'}"
            f"{(' as ' + player.get('role')) if player.get('role') else ''}. Main heroes: {hero_bits(player.get('heroes', []), 6)}."
        )

    # --- Hero section ---
    heroes = result.get("heroes", []) or []
    if heroes:
        hero = heroes[0]
        partners = _machine_chat_join([row.get("partner", "") for row in hero.get("pair_partners", [])], 5)
        section_map["hero"] = (
            f"{hero.get('hero') or 'That hero'} for {hero.get('team') or result.get('personal_team') or 'our team'}: "
            f"{hero.get('played_count', 0)} plays, {hero.get('played_wins', 0)}-{hero.get('played_losses', 0)}, "
            f"banned {hero.get('ban_count', 0)}x, protected {hero.get('protect_count', 0)}x. "
            f"Best partners: {partners}."
        )

    # --- Teams section ---
    teams = result.get("teams", []) or []
    team_profile_visuals: dict = {}
    if teams:
        team = teams[0]
        team_name = team.get("team") or "That team"
        bias = hero_bits(team.get("hero_bias", []), 8)
        pair_cores = []
        for row in (team.get("pair_cores", []) or [])[:5]:
            left = (row.get("hero_a") or "").strip()
            right = (row.get("hero_b") or "").strip()
            co = row.get("co_appearances", 0)
            if left and right:
                pair_cores.append(f"{left}/{right} ({co}x)")
        map_stats = []
        for row in (team.get("map_stats", []) or [])[:5]:
            map_name = (row.get("map_name") or "").strip()
            if map_name:
                w = row.get("wins", 0)
                l = row.get("losses", 0)
                played = row.get("played", 0)
                map_stats.append(f"{map_name} {w}-{l}/{played}")
        # Player pools
        player_lines = []
        for pp in (team.get("player_pools") or [])[:6]:
            pname = pp.get("player_name", "")
            role = pp.get("role", "")
            top_h = hero_bits(pp.get("heroes", []), 4)
            if pname:
                player_lines.append(f"{pname}{(' (' + role + ')') if role else ''}: {top_h}")
        team_has_data = bool(bias) or bool(pair_cores) or bool(map_stats)
        data_season = team.get("data_season") or team.get("season")
        season_note = ""
        if data_season and data_season != season_value and season_value and season_value != "all":
            season_note = f" [data is from season {data_season} — no ETL data exists yet for season {season_value}]"
        if team_has_data:
            team_text = (
                f"{team.get('team') or 'That team'} profile{season_note}:\n"
                f"  Hero bias: {bias}.\n"
                f"  Core pairs: {_machine_chat_join(pair_cores, 5)}.\n"
                f"  Map record: {_machine_chat_join(map_stats, 5)}."
            )
        else:
            team_text = f"No draft-engine data loaded yet for {team.get('team') or 'that team'} — comp data will appear after the ETL runs."
        if wants_bans:
            ban_rows = sorted(
                [row for row in (team.get("hero_bias") or []) if (row.get("ban_count") or 0) > 0],
                key=lambda row: row.get("ban_count", 0),
                reverse=True,
            )
            ban_text = _machine_chat_join(
                [f"{row.get('hero', '')} ({row.get('ban_count', 0)}x)" for row in ban_rows if row.get("hero")],
                6,
            )
            if ban_text:
                team_text += f"\n  Most banned: {ban_text}."
        if player_lines:
            team_text += "\n  Player pools:\n    " + "\n    ".join(player_lines)
        section_map["team"] = team_text

        if wants_comfort:
            top_bias_rows = [row for row in (team.get("hero_bias") or []) if row.get("hero")][:5]
            top_bias_text = hero_bits(top_bias_rows, 5)
            top_player_rows = []
            for pp in (team.get("player_pools") or [])[:4]:
                pname = pp.get("player_name", "")
                heroes_text = hero_bits(pp.get("heroes") or [], 3)
                if pname and heroes_text:
                    top_player_rows.append(f"{pname}: {heroes_text}")
            if top_bias_text or top_player_rows:
                comfort_parts = []
                if top_bias_text:
                    comfort_parts.append(f"{team_name}'s most played heroes are {top_bias_text}.")
                if top_player_rows:
                    comfort_parts.append("Key player pools: " + "; ".join(top_player_rows) + ".")
                section_map["comfort"] = " ".join(comfort_parts)

        if wants_map and not section_map.get("map"):
            ranked_maps = []
            for row in (team.get("map_stats") or []):
                played = row.get("played", 0) or 0
                wins = row.get("wins", 0) or 0
                losses = row.get("losses", 0) or 0
                total = wins + losses
                if not played and not total:
                    continue
                denominator = total or played
                win_rate = (wins / denominator) if denominator else 0
                ranked_maps.append((win_rate, denominator, row))
            ranked_maps.sort(key=lambda item: (-item[0], -item[1], (item[2].get("map_name") or "")))
            if ranked_maps:
                best_rows = [item[2] for item in ranked_maps[:3]]
                map_bits = []
                for row in best_rows:
                    map_name = row.get("map_name", "")
                    wins = row.get("wins", 0) or 0
                    losses = row.get("losses", 0) or 0
                    total = wins + losses
                    wr = round((wins / total) * 100, 1) if total else 0
                    if map_name:
                        map_bits.append(f"{map_name} ({wins}-{losses}, {wr}% WR)")
                if map_bits:
                    section_map["map"] = f"Best maps for {team_name}: " + ", ".join(map_bits) + "."

        if wants_player and heroes:
            hero_name = (heroes[0].get("hero") or "").strip().lower()
            hero_players = []
            for pp in (team.get("player_pools") or []):
                pname = pp.get("player_name", "")
                role = pp.get("role", "")
                for hero_row in (pp.get("heroes") or []):
                    if (hero_row.get("hero") or "").strip().lower() != hero_name:
                        continue
                    appearances = hero_row.get("appearances", 0) or 0
                    wins = hero_row.get("wins", 0) or 0
                    losses = hero_row.get("losses", 0) or 0
                    total = wins + losses
                    detail = f"{pname}{(' (' + role + ')') if role else ''}: {appearances} maps"
                    if total:
                        detail += f", {round((wins / total) * 100, 1)}% WR"
                    hero_players.append(detail)
                    break
            if hero_players:
                section_map["player"] = (
                    f"Players on {team_name} with {heroes[0].get('hero') or 'that hero'}: "
                    + "; ".join(hero_players[:5])
                    + "."
                )

        if heroes and section_map.get("hero") and wants_bans:
            hero_name = (heroes[0].get("hero") or "").strip().lower()
            impacted_players = []
            for pp in (team.get("player_pools") or []):
                pname = (pp.get("player_name") or "").strip()
                if not pname:
                    continue
                hero_rows = pp.get("heroes") or []
                for h_row in hero_rows:
                    h_name = (h_row.get("hero") or "").strip().lower()
                    if h_name != hero_name:
                        continue
                    appearances = h_row.get("appearances", 0) or 0
                    wins = h_row.get("wins", 0) or 0
                    losses = h_row.get("losses", 0) or 0
                    total = wins + losses
                    if total > 0:
                        wr = round((wins / total) * 100, 1)
                        impacted_players.append(f"{pname} ({appearances} maps, {wr}% WR)")
                    else:
                        impacted_players.append(f"{pname} ({appearances} maps)")
                    break
            if impacted_players:
                section_map["hero"] += (
                    " Hero-pool impact if banned: this displaces "
                    + _machine_chat_join(impacted_players, 4)
                    + "."
                )
            else:
                section_map["hero"] += (
                    " Hero-pool impact if banned: no strong one-trick signal in current team pool, "
                    "so ban value is mostly comp-denial rather than player displacement."
                )

        # Build structured team profile for card rendering
        team_profile_visuals = {
            "team_name": team.get("team") or "",
            "hero_bias": [
                {
                    "hero": b.get("hero", ""),
                    "played": b.get("played_count", 0),
                    "wins": b.get("played_wins", 0),
                    "losses": b.get("played_losses", 0),
                    "banned": b.get("ban_count", 0),
                }
                for b in (team.get("hero_bias") or [])[:10]
                if b.get("hero")
            ],
            "pair_cores": [
                {"hero_a": r.get("hero_a", ""), "hero_b": r.get("hero_b", ""), "co_appearances": r.get("co_appearances", 0)}
                for r in (team.get("pair_cores") or [])[:5]
                if r.get("hero_a") and r.get("hero_b")
            ],
            "map_stats": [
                {"map_name": r.get("map_name", ""), "wins": r.get("wins", 0), "losses": r.get("losses", 0), "played": r.get("played", 0)}
                for r in (team.get("map_stats") or [])[:6]
                if r.get("map_name")
            ],
            "player_pools": [
                {
                    "player_name": pp.get("player_name", ""),
                    "role": pp.get("role", ""),
                    "heroes": [
                        {"hero": h.get("hero", ""), "appearances": h.get("appearances", 0), "wins": h.get("wins", 0), "losses": h.get("losses", 0)}
                        for h in (pp.get("heroes") or [])[:5]
                        if h.get("hero")
                    ],
                }
                for pp in (team.get("player_pools") or [])[:8]
                if pp.get("player_name")
            ],
        }

    # --- Map section ---
    maps = result.get("maps", []) or []
    if maps:
        map_row = maps[0]
        section_map["map"] = (
            f"On {map_row.get('map_name') or 'that map'}, {map_row.get('team') or result.get('personal_team') or 'our team'} is "
            f"{map_row.get('wins', 0)}-{map_row.get('losses', 0)} over {map_row.get('played', 0)} maps. "
            f"Most played: {hero_bits(map_row.get('hero_freq', []), 5)}. "
            f"Most common bans: {hero_bits(map_row.get('bans', []), 4)}."
        )

    # --- Scrim history section ---
    scrims = result.get("scrims_summary", []) or []
    scrim_rows_for_card: list = []
    if scrims:
        snippets = []
        for row in scrims[:5]:
            enemy = row.get("enemy_team") or row.get("opponent") or "unknown"
            scrim_date = row.get("scrim_date") or "undated"
            maps_played = row.get("maps", []) or []
            wins = sum(1 for m in maps_played if (m.get("result") or "").lower() == "win")
            losses = sum(1 for m in maps_played if (m.get("result") or "").lower() == "loss")
            result_str = f" ({wins}-{losses})" if wins + losses else ""
            snippets.append(f"{scrim_date} vs {enemy}{result_str}")
            scrim_rows_for_card.append({"date": scrim_date, "opponent": enemy, "wins": wins, "losses": losses})
        section_map["scrims"] = f"Recent scrim history: {'; '.join(snippets)}."

    # Build preferred ordering based on question type
    sections: list[str] = []
    seen: set[str] = set()
    preferred = []
    if wants_player:
        preferred.append("player")
    if wants_hero or (section_map.get("hero") and wants_bans):
        preferred.append("hero")
    if wants_comfort:
        preferred.append("comfort")
    if wants_map:
        preferred.append("map")
    if wants_scrims:
        preferred.append("scrims")
    if wants_team:
        preferred.extend(["team", "player"])
        if wants_scrims:
            preferred.append("scrims")
    # always include anything available in fallback order
    preferred.extend(["player", "hero", "team", "map"])
    if not wants_comps:
        preferred.append("scrims")
    for key in preferred:
        if key in seen or not section_map.get(key):
            continue
        if wants_scrims and key == "team" and not wants_team:
            continue
        sections.append(section_map[key])
        seen.add(key)
        if len(sections) >= 5:
            break

    if not sections:
        return None

    # Only attach the team profile card visuals when we have team data
    profile_visuals: dict = {}
    if teams and team_profile_visuals:
        profile_visuals["team_profile"] = team_profile_visuals
        if scrim_rows_for_card:
            profile_visuals["team_profile"]["recent_scrims"] = scrim_rows_for_card

    # Helper: load ban impact rows for a player name
    def _load_ban_impact(player_name: str) -> list[dict]:
        try:
            from draft_engine.site_context import get_scrim_history
            scrims = get_scrim_history(get_db(), result.get("personal_team") or "", vs=None, season=season, limit=100)
            rows = build_player_ban_impact(player_name, scrims)
            return rows
        except Exception:
            return []

    # Player profile card visual
    if players:
        p = players[0]
        ban_rows = _load_ban_impact(p.get("player_name", ""))
        profile_visuals["player_profile"] = {
            "player_name": p.get("player_name", ""),
            "team_name": p.get("team_name", ""),
            "role": p.get("role", ""),
            "heroes": [
                {
                    "hero": h.get("hero", ""),
                    "appearances": h.get("appearances", 0),
                    "wins": h.get("wins", 0),
                    "losses": h.get("losses", 0),
                }
                for h in (p.get("heroes") or [])[:10]
                if h.get("hero")
            ],
            "ban_impact": [
                {
                    "hero": r["hero"],
                    "hero_maps": r["hero_maps"],
                    "hero_wr": r["hero_wr"],
                    "times_banned": r["times_banned"],
                    "wr_when_banned": r["wr_when_banned"],
                    "wr_delta": r["wr_delta"],
                    "pivots": [
                        {"hero": pv["hero"], "count": pv["count"], "wr": pv.get("wr")}
                        for pv in (r.get("all_pivots") or [])[:4]
                    ],
                }
                for r in ban_rows[:6]
                if r.get("hero")
            ],
        }

    # Hero profile card visual
    if heroes:
        h = heroes[0]
        hero_name_key = (h.get("hero") or "").strip().lower()
        # Find which players on the personal team play this hero, gather their ban impact row for it
        hero_player_impacts: list[dict] = []
        try:
            from draft_engine.site_context import get_scrim_history
            scrims_for_hero = get_scrim_history(get_db(), result.get("personal_team") or "", vs=None, season=season, limit=100)
            # Get all players on personal team
            rows_players = get_db().execute(
                """SELECT p.name FROM players p
                   JOIN teams t ON p.team_id = t.id
                   WHERE t.is_personal = 1"""
            ).fetchall()
            for pr in rows_players:
                pname = pr[0]
                bi_rows = build_player_ban_impact(pname, scrims_for_hero)
                for bi in bi_rows:
                    if (bi.get("hero") or "").strip().lower() == hero_name_key and bi.get("hero_maps", 0) >= 3:
                        hero_player_impacts.append({
                            "player_name": pname,
                            "hero_maps": bi["hero_maps"],
                            "hero_wr": bi["hero_wr"],
                            "times_banned": bi["times_banned"],
                            "wr_when_banned": bi["wr_when_banned"],
                            "wr_delta": bi["wr_delta"],
                            "pivots": [
                                {"hero": pv["hero"], "count": pv["count"], "wr": pv.get("wr")}
                                for pv in (bi.get("all_pivots") or [])[:4]
                            ],
                        })
        except Exception:
            pass

        profile_visuals["hero_profile"] = {
            "hero": h.get("hero", ""),
            "team": h.get("team", ""),
            "played_count": h.get("played_count", 0),
            "played_wins": h.get("played_wins", 0),
            "played_losses": h.get("played_losses", 0),
            "ban_count": h.get("ban_count", 0),
            "protect_count": h.get("protect_count", 0),
            "pair_partners": [
                {"partner": pp.get("partner", ""), "co_appearances": pp.get("co_appearances", 0), "wins": pp.get("wins", 0)}
                for pp in (h.get("pair_partners") or [])[:8]
                if pp.get("partner")
            ],
            "player_impacts": hero_player_impacts,
        }

    profile_links: list[dict] = []
    if teams:
        team_name = (teams[0].get("team") or "").strip()
        if team_name:
            team_row = get_db().execute("SELECT id, name FROM teams WHERE lower(name)=lower(?) LIMIT 1", (team_name,)).fetchone()
            if team_row:
                try:
                    team_url = url_for("team_detail", team_id=team_row["id"])
                except Exception:
                    team_url = f"/team/{team_row['id']}"
                profile_links.append({
                    "type": "team",
                    "label": f"Open {team_row['name']} team profile",
                    "url": team_url,
                })
    if players:
        player_name = (players[0].get("player_name") or "").strip()
        player_team = (players[0].get("team_name") or "").strip()
        if player_name and player_team:
            player_row = get_db().execute(
                """SELECT p.id AS player_id, t.id AS team_id, p.name AS player_name
                   FROM players p JOIN teams t ON p.team_id = t.id
                   WHERE lower(t.name)=lower(?) AND lower(p.name)=lower(?)
                   LIMIT 1""",
                (player_team, player_name),
            ).fetchone()
            if player_row:
                try:
                    player_url = url_for("player_detail", team_id=player_row["team_id"], player_id=player_row["player_id"])
                except Exception:
                    player_url = f"/team/{player_row['team_id']}/player/{player_row['player_id']}"
                profile_links.append({
                    "type": "player",
                    "label": f"Open {player_row['player_name']} player profile",
                    "url": player_url,
                })

    return {
        "answer": "\n\n".join(sections),
        "meta": {
            "has_matchup": False,
            "intent": "site_info",
            "needs_context": False,
            "visuals": profile_visuals,
            "profile_links": profile_links,
            "site_search": result,
        },
    }


def _machine_agent_intent(message: str) -> str:
    q = (message or "").lower()
    hero_name = _machine_agent_parse_hero(message)
    if any(word in q for word in ("pivot", "swap", "switch", "go to", "play if", "likely to")) and any(word in q for word in ("ban", "banned", "deny", "remove")):
        return "player_pivot"
    if _machine_agent_parse_slot_compare(message):
        return "slot_compare"
    if _machine_agent_parse_hero(message) and any(word in q for word in ("volatility", "volatile", "swing")):
        return "hero_volatility"
    if any(word in q for word in ("map", "maps", "convoy", "domination", "convergence")):
        return "map"
    if any(phrase in q for phrase in ("next pick", "likely pick", "first pick", "pick next")):
        return "next_pick"
    if any(phrase in q for phrase in ("enemy comp", "enemy comps", "full enemy", "likely full", "they still get", "can they still get", "comps can they",
                                        "what comps do", "what comp do", "what comps can", "what comp can",
                                        "comps do they", "comp do they", "comps will they", "comp will they",
                                        "comps does", "comp does", "comps get", "comp get",
                                        "comps are available", "comp is available", "available comps",
                                        "comps for them", "their comps", "their comp options")):
        return "enemy_comps"
    if any(phrase in q for phrase in ("top 4 likely bans", "top four likely bans", "likely bans after", "after our first ban", "after my first ban", "after first ban")):
        return "ban_impact"
    if hero_name and any(word in q for word in ("ban", "banned", "remove", "deny")) and any(phrase in q for phrase in ("what if", "if i", "if we", "first")):
        return "ban_impact"
    if any(phrase in q for phrase in ("ban impact", "impact of ban", "if we ban", "banning impact")) or ("banning" in q and "matter" in q):
        return "ban_impact"
    if any(phrase in q for phrase in ("pivot", "pivot prediction", "swap to")):
        return "pivot"
    if any(
        phrase in q
        for phrase in (
            "what is their coach trying",
            "what are they trying to draft",
            "what is he trying to draft",
            "what is she trying to draft",
            "coach trying",
            "expected comp outcome",
            "expected comp outcomes",
            "draft tree",
            "comp tree",
            "coach read",
            "mind read",
        )
    ):
        return "coach_tree"

    # Strategic analysis taxonomy (identity / matchup theory / tradeoffs / failure / coaching).
    if any(
        phrase in q
        for phrase in (
            "draft identity",
            "identity",
            "fundamentally trying",
            "conditions must be true",
            "assumptions",
            "hidden weakness",
            "pressure point",
            "fight environments",
            "matchup",
            "interaction determines",
            "controls engagement",
            "controls spacing",
            "longer fights",
            "chaotic fights",
            "ult economy",
            "easier execution",
            "tradeoff",
            "what pivots remain",
            "adaptation",
            "strategic shift",
            "flexibility remains",
            "remaining adaptation options",
            "failure condition",
            "collapsed first",
            "draft structure or execution",
            "execution burden",
            "coordination",
            "common mistakes",
            "coach",
            "communication requirements",
            "player tendencies",
            "team historically prefer",
            "tendencies under pressure",
            "strategic habits",
            "tempo",
            "snowballing",
            "forced reactions",
            "timing window",
            "pace of adaptation",
            "pressure cycle",
            "stability",
            "high variance",
            "resilient",
            "collapse quickly",
            "disciplined teams",
            "misconception",
            "evidence conflicts",
            "supported but not proven",
            "overvalued",
            "undervalued",
            "remaining risks",
        )
    ):
        return "check"

    if any(
        phrase in q
        for phrase in (
            "how confident",
            "confidence level",
            "confidence",
            "sample size confidence",
        )
    ):
        return "confidence"
    if any(word in q for word in ("comp", "composition", "play", "run", "target")):
        return "comp"
    if any(word in q for word in ("risk", "risks", "watch", "danger", "threat", "volatile")):
        return "risk"
    if any(word in q for word in ("protect", "protects", "keep open")):
        return "protect"
    if any(word in q for word in ("comfort", "core", "hero pool", "pool")):
        return "comfort"
    if any(word in q for word in ("contested", "overlap", "shared")):
        return "contested"
    if any(word in q for word in ("stat", "stats", "win", "wr", "rate", "record", "history", "data")):
        return "stats"
    if any(word in q for word in ("force", "path", "route", "tree", "draft check", "check")):
        return "check"
    if any(word in q for word in ("ban", "bans", "counter", "deny")):
        return "ban"
    return "summary"


def _machine_agent_filter_visuals(intent: str, visuals: dict) -> dict:
    visuals = visuals or {}
    keys_by_intent = {
        "ban": ("recommended_bans", "enemy_comfort", "volatile"),
        "protect": ("recommended_protects", "target_comp", "contested"),
        "comp": ("target_comp", "our_comp_rows", "recommended_protects", "enemy_comfort"),
        "risk": ("volatile", "contested", "enemy_comfort", "recommended_bans"),
        "comfort": ("our_comfort", "enemy_comfort"),
        "contested": ("contested", "recommended_bans", "target_comp"),
        "stats": ("recommended_bans", "our_comfort", "enemy_comfort", "target_comp"),
        "slot_compare": ("slot_compare",),
        "next_pick": ("likely_next_pick", "target_comp", "enemy_comfort"),
        "enemy_comps": ("enemy_comps", "recommended_bans", "enemy_comfort", "our_comfort"),
        "ban_impact": ("hero_focus", "recommended_bans", "likely_next_pick", "target_comp", "enemy_comfort", "our_comfort", "volatile_rows"),
        "player_pivot": ("player_pivot",),
        "pivot": ("pivot_predictions", "recommended_bans"),
        "coach_tree": ("coach_read", "draft_tree", "comp_tree", "enemy_comps", "pivot_predictions", "recommended_bans"),
        "confidence": ("confidence", "target_comp", "recommended_bans"),
        "hero_volatility": ("hero_focus", "volatile_rows"),
        "check": ("recommended_bans", "recommended_protects", "target_comp", "enemy_comfort", "contested", "volatile"),
        "summary": ("recommended_bans", "target_comp", "enemy_comfort"),
    }
    selected = {key: visuals.get(key, []) for key in keys_by_intent.get(intent, ()) if visuals.get(key)}
    if visuals.get("ban_candidate_details"):
        selected["ban_candidate_details"] = visuals["ban_candidate_details"]
    if visuals.get("protect_candidate_details"):
        selected["protect_candidate_details"] = visuals["protect_candidate_details"]
    if visuals.get("enemy_expected_bans"):
        selected["enemy_expected_bans"] = visuals["enemy_expected_bans"]
    if visuals.get("live_decision"):
        selected["live_decision"] = visuals["live_decision"]
    if intent == "map":
        selected["map_consensus"] = visuals.get("map_consensus", [])
    return selected


def _machine_agent_answer_for_intent(message: str, context_text: str, meta: dict, intent: str) -> str:
    if not meta.get("has_matchup"):
        return "Pick both teams in the agent controls and I can produce a matchup-specific draft read."

    visuals = meta.get("visuals") or {}
    ban_line = _machine_chat_join(visuals.get("recommended_bans", []), 4)
    protect_line = _machine_chat_join(visuals.get("recommended_protects", []), 3)
    comp_line = _machine_chat_join(visuals.get("target_comp", []), 6)
    enemy_line = _machine_chat_join(visuals.get("enemy_comfort", []), 4)
    our_line = _machine_chat_join(visuals.get("our_comfort", []), 4)
    next_pick = _machine_chat_join(visuals.get("likely_next_pick", []), 2)
    contested_line = _machine_chat_join(visuals.get("contested", []), 4)
    volatile_rows = visuals.get("volatile_rows", []) or []
    volatile_line = ", ".join(
        f"{row.get('hero')} ({row.get('favored_side')}, delta {row.get('delta', 0)})"
        for row in volatile_rows[:3]
        if row.get("hero")
    ) or _machine_chat_join(visuals.get("volatile", []), 3)
    enemy_comps = visuals.get("enemy_comps", []) or []
    pivot_predictions = visuals.get("pivot_predictions", []) or []
    coach_read = visuals.get("coach_read") or {}
    draft_tree = visuals.get("draft_tree") or {}
    comp_tree = visuals.get("comp_tree") or {}
    confidence = visuals.get("confidence") or {}
    map_rows = visuals.get("map_consensus", []) or []
    ban_candidate_details = visuals.get("ban_candidate_details", []) or []
    protect_candidate_details = visuals.get("protect_candidate_details", []) or []

    lines = context_text.splitlines()
    if intent == "ban":
        ban_seq = (visuals.get("recommended_bans") or [])[:4]
        seq_line = " | ".join(f"Ban {i+1}: {h}" for i, h in enumerate(ban_seq))
        seq_part = ("Likely sequence: " + seq_line + ".\n\n") if seq_line else ""
        detail_lines = []
        for row in ban_candidate_details[:3]:
            hero = row.get("hero", "")
            if not hero:
                continue
            anova = row.get("anova_significance") or {}
            anova_text = ""
            if anova:
                anova_text = (
                    f", ANOVA {anova.get('significance', 'n/a')}"
                    f" F={anova.get('f_stat', 0)}"
                    f" lift={anova.get('loss_lift_pp', 0)}pp"
                )
            permutation = row.get("permutation_importance") or {}
            permutation_text = ""
            if permutation:
                permutation_text = (
                    f", permutation {permutation.get('importance', 'n/a')}"
                    f" rel={permutation.get('relative_lift', 0)}"
                )
            detail_lines.append(
                f"- {hero} ({row.get('role') or row.get('role_alias') or 'Hero'}): "
                f"score {row.get('ban_score', 0)}, net {row.get('strategic_net_value', 0)}, "
                f"enemy {row.get('strategic_enemy_value', 0)}, our cost {row.get('strategic_our_value', 0)}"
                f"{anova_text}"
                f"{permutation_text}"
                + (f" - {row.get('model_hint')}" if row.get("model_hint") else "")
            )
        detail_part = ("Marvel Rivals candidate model:\n" + "\n".join(detail_lines) + "\n\n") if detail_lines else ""
        return (
            f"Top likely bans right now: {ban_line}.\n\n"
            f"{seq_part}"
            f"{detail_part}"
            f"If you name your first ban, I can re-rank this sequence conditionally."
        )
    elif intent == "protect":
        detail_lines = []
        for row in protect_candidate_details[:3]:
            hero = row.get("hero", "")
            if not hero:
                continue
            detail_lines.append(
                f"- {hero} ({row.get('role') or row.get('role_alias') or 'Hero'}): "
                f"score {row.get('protect_score', 0)}, net {row.get('strategic_net_value', 0)}, "
                f"our value {row.get('strategic_our_value', 0)}, enemy value {row.get('strategic_enemy_value', 0)}"
                + (f" - {row.get('model_hint')}" if row.get("model_hint") else "")
            )
        detail_part = ("\n\nMarvel Rivals protect model:\n" + "\n".join(detail_lines)) if detail_lines else ""
        return (
            f"Protect {protect_line or 'our core enablers'} first.\n\n"
            f"That keeps {comp_line or 'the target comp'} live."
            f"{detail_part}"
        )
    elif intent == "comp":
        our_comp_rows = visuals.get("our_comp_rows") or []
        if our_comp_rows:
            ranked = []
            for i, r in enumerate(our_comp_rows, 1):
                heroes = _machine_chat_join(r.get("heroes", []), 6)
                wr = r.get("win_rate", 0)
                rate = r.get("rate", 0)
                ranked.append(f"{i}. {heroes} ({wr}% WR, {rate}% pick rate)")
            comp_list = "\n".join(ranked)
            return (
                f"Best comps for us:\n{comp_list}\n\n"
                f"Lead with protects on {protect_line or 'the core anchors'} and open with {ban_line or enemy_line} on their side."
            )
        return (
            f"Lean into {comp_line or 'the strongest available route'}.\n\n"
            f"Use protects on {protect_line or 'the key anchors'} and make them answer {ban_line or enemy_line}."
        )
    elif intent == "map":
        pivot_rows = pivot_predictions
        if map_rows:
            first_row = map_rows[0] if len(map_rows) > 0 else {}
            second_row = map_rows[1] if len(map_rows) > 1 else first_row
            best_maps = _machine_chat_join(first_row.get("maps", []), 3)
            safe_maps = _machine_chat_join(second_row.get("maps", []), 3)
            pivot = pivot_rows[0] if pivot_rows else {}
            pivot_text = _machine_chat_join(pivot.get("pivot", []), 5) if pivot else "no strong pivot row"
            counter_text = _machine_chat_join(pivot.get("counter", []), 5) if pivot else "lean on the core comp path"
            return (
                f"Look for {best_maps}.\n\n"
                f"Fallback is {safe_maps}. If they pivot into {pivot_text}, answer with {counter_text}."
            )
    elif intent == "risk":
        return (
            f"The swing pieces here are {volatile_line or 'their flex threats'}.\n\n"
            f"If you want the safer draft, start with {ban_line or contested_line}."
        )
    elif intent == "comfort":
        return (
            f"Our comfort lane is {our_line or 'not clear enough yet'}.\n\n"
            f"Their comfort is {enemy_line or 'not clear enough yet'}, so draft around that overlap first."
        )
    elif intent == "contested":
        return (
            f"The real fight is over {contested_line or 'the shared core'}.\n\n"
            f"If you do not want to fight there, ban {ban_line or 'the shared core'} and keep {comp_line or 'our route'} open."
        )
    elif intent == "coach_tree":
        intent_line = coach_read.get("intent") or "preserve their strongest available comp route"
        break_point = _machine_chat_join(coach_read.get("break_point", []), 1) or ban_line or "their route anchor"
        primary = _machine_chat_join(coach_read.get("expected_primary_comp", []), 6)
        pivot = _machine_chat_join(coach_read.get("expected_pivot", []), 6)
        answer = _machine_chat_join(coach_read.get("our_answer", []), 6)
        tree_branches = draft_tree.get("branches") or []
        outcome_rows = comp_tree.get("outcomes") or []
        branch_line = "; ".join(
            f"{row.get('label')}: {row.get('outcome')}"
            for row in tree_branches[:3]
            if row.get("label") or row.get("outcome")
        )
        outcome_line = "; ".join(
            f"{row.get('label')}: {_machine_chat_join(row.get('heroes', []), 6)}"
            for row in outcome_rows[:3]
            if row.get("label") or row.get("heroes")
        )
        return (
            f"Their coach is most likely trying to {intent_line}.\n\n"
            f"Break point: {break_point}.\n\n"
            f"Expected comp outcomes: primary {primary or 'unclear'}, pivot {pivot or 'no strong pivot sample'}, our answer {answer or comp_line or 'target comp not clear'}.\n\n"
            f"Draft tree: {branch_line or 'not enough data for branching'}.\n\n"
            f"Comp tree: {outcome_line or 'not enough comp data'}."
        )
    elif intent == "check":
        return (
            f"Right now I would ban {ban_line or 'their pressure pieces'} and keep {comp_line or 'our main route'} live.\n\n"
            f"Main swing check: {volatile_line or enemy_line or 'their comfort core'}."
        )
    elif intent == "stats":
        sample = confidence.get("sample", 0)
        return (
            f"On the numbers, {comp_line or 'the route'} is still best.\n\n"
            f"This read is coming from {sample} filtered records."
        )
    elif intent == "slot_compare":
        return "Use the compare view here.\n\nThe board below shows which slot actually performs better."
    elif intent == "next_pick":
        return (
            f"Next I would expect {next_pick or 'the next route anchor'}.\n\n"
            f"That keeps {comp_line or 'the route'} open and pairs with {protect_line or enemy_line}."
        )
    elif intent == "enemy_comps":
        q_lower = (message or "").lower()
        # Resolve which team is the subject — prefer an explicitly named team_b
        subject = meta.get("team_b") or "them"
        if enemy_comps:
            ranked = []
            for i, r in enumerate(enemy_comps, 1):
                heroes = _machine_chat_join(r.get("heroes", []), 6)
                wr = r.get("win_rate", 0)
                rate = r.get("rate", 0)
                ranked.append(f"{i}. {heroes} ({wr}% WR{(', ' + str(rate) + '% pick rate') if rate else ''})")
            comp_list = "\n".join(ranked)
            return (
                f"Best comp options for {subject}:\n{comp_list}\n\n"
                f"They lean on {enemy_line or 'their comfort core'}. Break it up with {ban_line or 'the first deny layer'}."
            )
        return (
            f"Read {subject}'s draft through {enemy_line or 'their comfort core'}, and keep {our_line or 'our comfort core'} available.\n\n"
            f"Use {ban_line or 'the deny layer'} to keep the full comp from settling."
        )
    elif intent == "ban_impact":
        hero = _machine_agent_parse_hero(message)
        hero_key = (hero or "").strip().lower()
        filtered_comp = _machine_chat_join(
            [item for item in visuals.get("target_comp", []) if (item or "").strip().lower() != hero_key],
            6,
        )
        filtered_followup = _machine_chat_join(
            [item for item in visuals.get("recommended_bans", []) if (item or "").strip().lower() != hero_key],
            4,
        )
        enemy_ban_line = _machine_chat_join(
            [item for item in visuals.get("recommended_bans", []) if (item or "").strip().lower() != hero_key],
            5,
        )
        ban_seq = [
            item for item in (visuals.get("recommended_bans", []) or [])
            if (item or "").strip().lower() != hero_key
        ][:4]
        seq_line = " | ".join(f"Ban {i+1}: {h}" for i, h in enumerate(ban_seq))
        seq_part = ("Likely sequence: " + seq_line + ".\n\n") if seq_line else ""
        next_pressure = _machine_chat_join(visuals.get("likely_next_pick", []), 2)
        return (
            f"If you first-ban {hero or 'that hero'}, their likely deny board becomes {enemy_ban_line or filtered_followup or enemy_line or 'their comfort layer'}.\n\n"
            f"{seq_part}"
            f"Comfort read after that ban: they still lean on {enemy_line or 'their comfort core'}, while we can route through {our_line or filtered_comp or 'our comfort core'}.\n\n"
            f"That usually shifts the draft toward {filtered_comp or enemy_line or 'the next best route'}"
            f"{('. Next pick pressure: ' + next_pressure + '.') if next_pressure else '.'}"
        )
    elif intent == "hero_volatility":
        hero = _machine_agent_parse_hero(message)
        return (
            f"{hero or 'That hero'} is a real swing piece if it sits in {volatile_line or contested_line}.\n\n"
            f"If you want the lower-variance draft, ban {ban_line or hero or 'it'} early."
        )
    elif intent == "pivot":
        if pivot_predictions:
            row = pivot_predictions[0]
            diff = row.get("diff_count", 0) or 0
            pivot_type = "round swap" if diff <= 2 else "pressure pivot"
            pivot_label = (
                f"{diff}-hero swap — likely a situational round adjustment"
                if diff <= 2
                else f"{diff}-hero rebuild — typically a losing-map response"
            )
            return (
                f"If they start on {_machine_chat_join(row.get('base', []), 4)}, expect the pivot into {_machine_chat_join(row.get('pivot', []), 4)}.\n"
                f"Read: {pivot_label} ({pivot_type}).\n\n"
                f"Our clean answer is {_machine_chat_join(row.get('counter', []), 4) or comp_line}."
            )
        return (
            f"Watch their comfort lane: {enemy_line or 'no clean pivot read yet'}.\n\n"
            f"Keep {ban_line or comp_line} ready as the answer."
        )
    elif intent == "confidence":
        return (
            f"The model leans toward {comp_line or 'the current route'}.\n\n"
            f"Confidence is {confidence.get('confidence', 0)}% across {confidence.get('sample', 0)} records."
        )
    else:
        return (
            f"Start on {ban_line or 'their comfort core'} and hold {comp_line or 'our best route'} open.\n\n"
            f"Main pressure point is {enemy_line or volatile_line or contested_line}."
        )


def _machine_agent_site_context_text(site_result: dict) -> str:
    """Flatten a site_context search result dict into a plain text block for the LLM system prompt."""
    lines = []
    personal = site_result.get("personal_team", "")
    lines.append(f"Personal/our team: {personal}")
    for p in (site_result.get("players") or []):
        heroes = ", ".join(
            f"{h.get('hero')} ({h.get('appearances', 0)} games, {h.get('wins', 0)}-{h.get('losses', 0)})"
            for h in (p.get("heroes") or [])[:8]
        )
        lines.append(f"Player {p.get('player_name')} on {p.get('team_name', '')} ({p.get('role', '')}): {heroes}")
    for h in (site_result.get("heroes") or []):
        partners = ", ".join(r.get("partner", "") for r in (h.get("pair_partners") or [])[:5])
        lines.append(
            f"Hero {h.get('hero')} for {h.get('team')}: played {h.get('played_count', 0)}, "
            f"{h.get('played_wins', 0)}-{h.get('played_losses', 0)}, "
            f"banned {h.get('ban_count', 0)}x, protected {h.get('protect_count', 0)}x. Partners: {partners}"
        )
    for t in (site_result.get("teams") or []):
        bias = ", ".join(
            f"{b.get('hero')} (played {b.get('played_count', 0)}, banned {b.get('ban_count', 0)})"
            for b in (t.get("hero_bias") or [])[:10]
        )
        pairs = ", ".join(
            f"{r.get('hero_a')}/{r.get('hero_b')} ({r.get('co_appearances', 0)}x)"
            for r in (t.get("pair_cores") or [])[:6]
        )
        map_recs = ", ".join(
            f"{r.get('map_name')} {r.get('wins', 0)}-{r.get('losses', 0)}"
            for r in (t.get("map_stats") or [])[:6]
        )
        lines.append(f"Team {t.get('team')}: bias [{bias}]. Pairs: {pairs}. Maps: {map_recs}")
        # Include player pools
        for pp in (t.get("player_pools") or [])[:8]:
            pp_heroes = ", ".join(
                f"{h.get('hero')} ({h.get('appearances', 0)})"
                for h in (pp.get("heroes") or [])[:6]
            )
            lines.append(
                f"  - {pp.get('player_name')}{(' (' + pp.get('role') + ')') if pp.get('role') else ''}: {pp_heroes}"
            )
    for m in (site_result.get("maps") or []):
        freq = ", ".join(f"{f.get('hero')} ({f.get('count', 0)})" for f in (m.get("hero_freq") or [])[:6])
        bans = ", ".join(f"{f.get('hero')} ({f.get('count', 0)})" for f in (m.get("bans") or [])[:5])
        lines.append(
            f"Map {m.get('map_name')} for {m.get('team')}: {m.get('wins', 0)}-{m.get('losses', 0)} over {m.get('played', 0)}. "
            f"Most picked: {freq}. Bans: {bans}"
        )
    for s in (site_result.get("scrims_summary") or [])[:8]:
        maps_played = s.get("maps", []) or []
        wins = sum(1 for m in maps_played if (m.get("result") or "").lower() == "win")
        losses = sum(1 for m in maps_played if (m.get("result") or "").lower() == "loss")
        result_str = f" {wins}-{losses}" if wins + losses else ""
        lines.append(f"Scrim {s.get('scrim_date', 'undated')} vs {s.get('enemy_team') or s.get('opponent', '?')}{result_str}")
    return "\n".join(lines)


def _machine_agent_llm_answer(message: str, context: str, personal_team: str, meta: dict | None = None, intent: str = "general", site_context_text: str = "", season: str = "all") -> str | None:
    """Generate a natural-language answer via the Minimax LLM."""
    try:
        from draft_engine.llm import build_draft_system_prompt, call_minimax_chat
        system_prompt = build_draft_system_prompt(
            context_text=context or "",
            site_context_text=site_context_text or "",
            personal_team=personal_team or "",
            meta=meta,
            intent=intent,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ]
        return call_minimax_chat(messages)
    except Exception:
        return None


def _machine_agent_humanize_answer(text: str) -> str:
    """Strip reasoning tags and normalize whitespace while preserving markdown emphasis."""
    if not text:
        return ""
    # Strip <think>…</think> blocks from MiniMax reasoning models
    cleaned = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _machine_agent_extract_season(message: str, fallback: str = "all") -> str:
    match = re.search(r"\b(?:season|patch|s)\s*([0-9]+(?:\.[0-9]+)?)\b", message or "", re.IGNORECASE)
    if match:
        return match.group(1)
    if re.search(r"\ball\s+seasons?\b", message or "", re.IGNORECASE):
        return "all"
    return fallback or "all"


def _machine_agent_extract_map(message: str, fallback: str = "all") -> str:
    text = (message or "").lower()
    for map_name in MAPS:
        if map_name.lower() in text:
            return map_name
    return fallback or "all"


def _machine_agent_find_mentioned_teams(message: str) -> list[dict]:
    text = (message or "").lower()
    rows = get_db().execute("SELECT id, name FROM teams ORDER BY LENGTH(name) DESC").fetchall()
    matches = []
    token_set = set(re.findall(r"[a-z0-9]+", text))

    def _team_aliases(team_name: str) -> set[str]:
        cleaned = re.sub(r"[^a-z0-9\s]", "", (team_name or "").lower()).strip()
        parts = [p for p in cleaned.split() if p and p not in {"the", "team"}]
        aliases: set[str] = set()
        if not parts:
            return aliases
        aliases.add(cleaned)
        aliases.add("".join(parts))
        # Initialism: Virtus Pro -> vp, Liquid Citadel -> lc
        if len(parts) > 1:
            aliases.add("".join(p[0] for p in parts if p))
        # Common short form: Sentinels -> sen
        if parts and len(parts[0]) >= 3:
            aliases.add(parts[0][:3])
        return {a for a in aliases if len(a) >= 2}

    for row in rows:
        name = (row["name"] or "").strip()
        if not name:
            continue
        name_l = name.lower()
        if name_l in text:
            matches.append({"id": int(row["id"]), "name": name, "pos": text.find(name_l)})
            continue
        # Alias/acronym match (e.g. VP, SEN, LC, 100T)
        aliases = _team_aliases(name)
        alias_hit = next((a for a in aliases if a in token_set), None)
        if alias_hit:
            matches.append({"id": int(row["id"]), "name": name, "pos": text.find(alias_hit)})

    # Deduplicate by team id if multiple aliases hit
    dedup = {}
    for row in matches:
        if row["id"] not in dedup or row["pos"] < dedup[row["id"]]["pos"]:
            dedup[row["id"]] = row
    matches = list(dedup.values())
    matches.sort(key=lambda row: row["pos"])
    return matches


def _machine_agent_get_personal_team() -> dict | None:
    preferred_row = get_db().execute(
        """
        SELECT id, name
        FROM teams
        WHERE COALESCE(quality_tag, '') = 'Preferred'
        ORDER BY COALESCE(sort_order, 0), name COLLATE NOCASE
        LIMIT 1
        """
    ).fetchone()
    if preferred_row:
        return dict(preferred_row)

    row = get_db().execute(
        "SELECT id, name FROM teams WHERE is_personal = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _machine_agent_context_from_payload(payload: dict, message: str) -> dict:
    raw_context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    personal_team = _machine_agent_get_personal_team()

    def int_or_none(value):
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    context = {
        "team_a_id": int_or_none(payload.get("team_a") or raw_context.get("team_a_id") or (personal_team or {}).get("id")),
        "team_b_id": int_or_none(payload.get("team_b") or raw_context.get("team_b_id")),
        "season": str(payload.get("season") or raw_context.get("season") or "all").strip(),
        "map": str(payload.get("map") or raw_context.get("map") or "all").strip(),
        "map_type": str(payload.get("map_type") or raw_context.get("map_type") or "all").strip(),
        "mode_type": str(payload.get("mode_type") or raw_context.get("mode_type") or "all").strip(),
        "include_scrims": bool(payload.get("include_scrims", raw_context.get("include_scrims", True))),
        "include_tournaments": bool(payload.get("include_tournaments", raw_context.get("include_tournaments", True))),
        "reasoning_mode": "reasoning",
    }

    # Optional live draft snapshot from the Draft Reasoner board.
    raw_draft_live = raw_context.get("draft_live")
    if isinstance(raw_draft_live, dict):
        phase_data = raw_draft_live.get("current_phase") or {}
        context["draft_live"] = {
            "active": bool(raw_draft_live.get("active")),
            "our_bans": list(raw_draft_live.get("our_bans") or []),
            "our_protects": list(raw_draft_live.get("our_protects") or []),
            "enemy_bans": list(raw_draft_live.get("enemy_bans") or []),
            "enemy_protects": list(raw_draft_live.get("enemy_protects") or []),
            "open_slots": list(raw_draft_live.get("open_slots") or []),
            "current_phase": {
                "phase_num": phase_data.get("phase_num"),
                "phase_label": phase_data.get("phase_label"),
                "next_team": phase_data.get("next_team"),
                "next_slot": phase_data.get("next_slot"),
                "next_action_type": phase_data.get("next_action_type"),
            }
        }

    context["season"] = _machine_agent_extract_season(message, context["season"])
    context["map"] = _machine_agent_extract_map(message, context["map"])
    text = (message or "").lower()
    if "scrim only" in text or "only scrim" in text:
        context["include_scrims"] = True
        context["include_tournaments"] = False
    elif "tournament only" in text or "only tournament" in text:
        context["include_scrims"] = False
        context["include_tournaments"] = True

    mentioned = _machine_agent_find_mentioned_teams(message)
    if len(mentioned) >= 2:
        if not context.get("team_a_id"):
            context["team_a_id"] = mentioned[0]["id"]
            context["team_b_id"] = mentioned[1]["id"]
        else:
            opposing_mentions = [row for row in mentioned if row["id"] != context.get("team_a_id")]
            if opposing_mentions:
                context["team_b_id"] = opposing_mentions[0]["id"]
            elif len(mentioned) > 1:
                context["team_b_id"] = mentioned[1]["id"]
    elif len(mentioned) == 1:
        team = mentioned[0]
        before = text[:team["pos"]]
        if context.get("team_a_id") and context.get("team_a_id") == team["id"]:
            pass
        elif re.search(r"\b(vs|versus|against|enemy|opp|opponent)\s*$", before):
            context["team_b_id"] = team["id"]
        elif re.search(r"\b(we are|we're|our team is|as|playing as)\s*$", before):
            context["team_a_id"] = team["id"]
        elif not context.get("team_a_id") and not context.get("team_b_id"):
            context["team_a_id"] = team["id"]
        # Otherwise keep the currently selected context intact.

    for key in ("team_a_id", "team_b_id"):
        if context.get(key):
            row = get_db().execute("SELECT name FROM teams WHERE id = ?", (context[key],)).fetchone()
            context[key.replace("_id", "_name")] = row["name"] if row else ""
    return context


def _machine_agent_draft_live_context_hint(chat_context: dict) -> str:
    draft_live = chat_context.get("draft_live")
    if not isinstance(draft_live, dict) or not draft_live.get("active"):
        return ""

    our_bans = [str(v).strip() for v in (draft_live.get("our_bans") or []) if str(v).strip()]
    our_protects = [str(v).strip() for v in (draft_live.get("our_protects") or []) if str(v).strip()]
    enemy_bans = [str(v).strip() for v in (draft_live.get("enemy_bans") or []) if str(v).strip()]
    enemy_protects = [str(v).strip() for v in (draft_live.get("enemy_protects") or []) if str(v).strip()]
    open_slots = [str(v).strip() for v in (draft_live.get("open_slots") or []) if str(v).strip()]
    current_phase = draft_live.get("current_phase") or {}
    phase_label = current_phase.get("phase_label", "")
    next_team = current_phase.get("next_team", "")

    lines = ["Live draft board state (from Draft Reasoner):"]
    if phase_label:
        team_label = "Our team" if next_team == "a" else ("Enemy team" if next_team == "b" else "")
        lines.append(f"- Current: {phase_label}, next turn is {team_label}")
    if our_bans:
        lines.append("- Our bans: " + ", ".join(our_bans[:6]))
    if our_protects:
        lines.append("- Our protects: " + ", ".join(our_protects[:4]))
    if enemy_bans:
        lines.append("- Enemy bans: " + ", ".join(enemy_bans[:6]))
    if enemy_protects:
        lines.append("- Enemy protects: " + ", ".join(enemy_protects[:4]))
    if open_slots:
        lines.append("- Open draft slots: " + ", ".join(open_slots[:8]))
    if current_phase:
        lines.append("- Do not name projected comps yet; wait until every draft phase is complete.")
    else:
        lines.append("- Draft phase is complete; projected comps may now be named.")
    lines.append("Use this board as live state for reasoning and adaptation analysis.")
    return "\n".join(lines)


def _machine_agent_live_decision_packet(chat_context: dict, visuals: dict | None) -> tuple[str, dict]:
    """Build a compact local read for live draft ban reasoning and pivot paths."""
    visuals = visuals or {}
    draft_live = chat_context.get("draft_live")
    if not isinstance(draft_live, dict) or not draft_live.get("active"):
        return "", {}

    team_a = chat_context.get("team_a_name") or "our team"
    team_b = chat_context.get("team_b_name") or "the enemy"
    current_phase = draft_live.get("current_phase") or {}
    next_team = current_phase.get("next_team")
    draft_complete = not current_phase
    locked = {
        _canonical_draft_hero(str(hero or "").strip()).lower()
        for hero in (
            list(draft_live.get("our_bans") or [])
            + list(draft_live.get("our_protects") or [])
            + list(draft_live.get("enemy_bans") or [])
            + list(draft_live.get("enemy_protects") or [])
        )
        if str(hero or "").strip()
    }
    def _clean_heroes(values, limit: int = 6) -> list[str]:
        heroes = []
        seen = set()
        for value in values or []:
            hero = _canonical_draft_hero(str(value or "").strip())
            key = hero.lower()
            if hero and key not in seen:
                heroes.append(hero)
                seen.add(key)
            if len(heroes) >= limit:
                break
        return heroes

    our_locked_bans = _clean_heroes(draft_live.get("our_bans"), 4)
    enemy_locked_bans = _clean_heroes(draft_live.get("enemy_bans"), 4)
    our_locked_protects = _clean_heroes(draft_live.get("our_protects"), 2)
    enemy_locked_protects = _clean_heroes(draft_live.get("enemy_protects"), 2)
    next_our_ban_slot_number = (len(our_locked_bans) + 1) if len(our_locked_bans) < 4 else 0
    next_our_ban_slot_key = f"ban{next_our_ban_slot_number}" if next_our_ban_slot_number else ""
    next_enemy_ban_slot_number = (len(enemy_locked_bans) + 1) if len(enemy_locked_bans) < 4 else 0
    next_enemy_ban_slot_key = f"ban{next_enemy_ban_slot_number}" if next_enemy_ban_slot_number else ""
    next_enemy_protect_slot_number = (len(enemy_locked_protects) + 1) if len(enemy_locked_protects) < 2 else 0
    next_enemy_protect_slot_key = f"protect{next_enemy_protect_slot_number}" if next_enemy_protect_slot_number else ""

    def _rank_historical_enemy_choices(
        slot_key: str,
        slot_history_lookup: dict | None,
        seeded_values: list[str],
        blocked_values: list[str],
        limit: int,
    ) -> list[str]:
        ranked: list[str] = []
        slot_history_lookup = slot_history_lookup or {}
        blocked_keys = {
            _canonical_draft_hero(str(hero or "").strip()).lower()
            for hero in blocked_values or []
            if str(hero or "").strip()
        }
        slot_scores = slot_history_lookup.get(slot_key) or {}
        historical_values = [
            hero_key
            for hero_key, _score in sorted(
                slot_scores.items(),
                key=lambda item: (float(item[1] or 0), item[0]),
                reverse=True,
            )
            if hero_key not in blocked_keys
        ]
        seeded_clean = [
            hero for hero in _clean_heroes(seeded_values, limit * 2)
            if hero.lower() not in blocked_keys
        ]
        for hero in historical_values + seeded_clean:
            canonical = _canonical_draft_hero(str(hero or "").strip())
            hero_key = canonical.lower() if canonical else ""
            if not canonical or not hero_key or hero_key in blocked_keys or hero_key in {item.lower() for item in ranked}:
                continue
            ranked.append(canonical)
            if len(ranked) >= limit:
                break
        return ranked

    candidate_bans = [
        hero for hero in _clean_heroes(visuals.get("recommended_bans"), 8)
        if hero.lower() not in locked
    ]
    candidate_detail_lookup = {
        str(row.get("hero", "") or "").strip().lower(): row
        for row in visuals.get("ban_candidate_details", []) or []
        if str(row.get("hero", "") or "").strip()
    }
    protect_detail_lookup = {
        str(row.get("hero", "") or "").strip().lower(): row
        for row in visuals.get("protect_candidate_details", []) or []
        if str(row.get("hero", "") or "").strip()
    }
    candidate_protects = [
        hero for hero in _clean_heroes(visuals.get("recommended_protects"), 5)
        if hero.lower() not in locked
    ]
    enemy_next_protects = _rank_historical_enemy_choices(
        next_enemy_protect_slot_key,
        visuals.get("enemy_protect_slot_history") or {},
        visuals.get("enemy_expected_protects") or [],
        our_locked_bans + enemy_locked_bans + our_locked_protects + enemy_locked_protects,
        4,
    )
    enemy_next_bans = _rank_historical_enemy_choices(
        next_enemy_ban_slot_key,
        visuals.get("enemy_ban_slot_history") or {},
        visuals.get("enemy_expected_bans") or [],
        our_locked_bans + enemy_locked_bans + our_locked_protects + enemy_locked_protects,
        4,
    )
    enemy_comfort_list = _clean_heroes(visuals.get("enemy_comfort"), 10)
    enemy_comfort = set(enemy_comfort_list)
    volatile_rows = {
        (row.get("hero") or "").strip(): row
        for row in visuals.get("volatile_rows", [])
        if (row.get("hero") or "").strip()
    }
    enemy_comp_rows = visuals.get("enemy_comps") or []
    pivot_rows = visuals.get("pivot_predictions") or []
    if not candidate_bans:
        fallback_pool = enemy_comfort_list + [
            hero for hero in volatile_rows
            if (volatile_rows.get(hero) or {}).get("favored_side") == team_b
        ]
        candidate_bans = [
            hero for hero in _clean_heroes(fallback_pool, 8)
            if hero.lower() not in locked
        ]

    next_ban_rows = []
    for hero in candidate_bans[:4]:
        closes = []
        for comp in enemy_comp_rows[:5]:
            heroes = _clean_heroes(comp.get("heroes"), 6)
            if hero in heroes:
                closes.append({
                    "heroes": heroes,
                    "rate": comp.get("rate", 0),
                    "win_rate": comp.get("win_rate", 0),
                    "confidence": comp.get("confidence", 0),
                })

        pivot_hits = []
        for row in pivot_rows[:6]:
            base = _clean_heroes(row.get("base"), 6)
            pivot = _clean_heroes(row.get("pivot"), 6)
            counter = _clean_heroes(row.get("counter"), 6)
            if hero in base:
                pivot_hits.append({
                    "type": "forces_pivot",
                    "from": base,
                    "to": pivot,
                    "counter": counter,
                    "diff_count": row.get("diff_count", 0),
                })
            elif hero in pivot:
                pivot_hits.append({
                    "type": "denies_pivot",
                    "from": base,
                    "to": pivot,
                    "counter": counter,
                    "diff_count": row.get("diff_count", 0),
                })

        reasons = []
        if closes:
            top = closes[0]
            if draft_complete:
                reasons.append(
                    f"hits enemy comp path {_machine_chat_join(top['heroes'], 4)}"
                    + (f" ({top.get('rate', 0)}% rate)" if top.get("rate") else "")
                )
            else:
                reasons.append(
                    "pressures an enemy route"
                    + (f" ({top.get('rate', 0)}% route signal)" if top.get("rate") else "")
                )
        if hero in enemy_comfort:
            reasons.append("removes enemy comfort/core access")
        volatile = volatile_rows.get(hero)
        if volatile:
            reasons.append(
                f"controls volatility favoring {volatile.get('favored_side', 'unknown')}"
                f" (delta {volatile.get('delta', 0)})"
            )
        if pivot_hits:
            hit = pivot_hits[0]
            if hit["type"] == "forces_pivot":
                reasons.append(
                    f"breaks their starting shell"
                    + (f" and pushes them toward {_machine_chat_join(hit.get('to', []), 3)}" if draft_complete else "")
                )
            else:
                reasons.append(
                    f"blocks their fallback"
                    + (f" into {_machine_chat_join(hit.get('to', []), 3)}" if draft_complete else "")
                )
        if not reasons:
            reasons.append("keeps pressure on the highest ranked remaining ban target")
        detail = candidate_detail_lookup.get(hero.lower()) or {}
        slot_history = (detail.get("historical_slot_bans") or {}).get(next_our_ban_slot_key, {}) if next_our_ban_slot_key else {}
        slot_hist_maps = int(slot_history.get("maps", 0) or 0)
        slot_hist_rate = float(slot_history.get("rate", 0) or 0)
        if detail.get("model_hint"):
            anova = detail.get("anova_significance") or {}
            anova_text = ""
            if anova:
                anova_text = (
                    f", ANOVA {anova.get('significance', 'n/a')}"
                    f" F={anova.get('f_stat', 0)}"
                    f" lift={anova.get('loss_lift_pp', 0)}pp"
                )
            permutation = detail.get("permutation_importance") or {}
            permutation_text = ""
            if permutation:
                permutation_text = (
                    f", permutation {permutation.get('importance', 'n/a')}"
                    f" rel={permutation.get('relative_lift', 0)}"
                )
            reasons.append(
                f"{detail.get('model_hint')} (score {detail.get('ban_score')}, net {detail.get('strategic_net_value')}{anova_text}{permutation_text})"
            )
        if next_our_ban_slot_key and slot_hist_maps > 0:
            reasons.insert(
                0,
                f"historically used in our {next_our_ban_slot_key} ({slot_hist_rate:.1f}% of draft maps, {slot_hist_maps} maps)",
            )

        next_ban_rows.append({
            "hero": hero,
            "detail": detail,
            "slot_hist_maps": slot_hist_maps,
            "slot_hist_rate": round(slot_hist_rate, 1),
            "reasons": reasons[:3],
            "closes": closes[:2],
            "pivot_paths": pivot_hits[:2],
        })

    if next_ban_rows:
        next_ban_rows.sort(
            key=lambda row: (
                1 if int(row.get("slot_hist_maps", 0) or 0) > 0 else 0,
                float(row.get("slot_hist_rate", 0) or 0),
                float(((row.get("detail") or {}).get("weighted_historical_priority", 0) or 0)),
                float(((row.get("detail") or {}).get("ban_score", 0) or 0)),
                float(((row.get("detail") or {}).get("strategic_net_value", 0) or 0)),
            ),
            reverse=True,
        )

    next_protect_rows = []
    for hero in candidate_protects[:3]:
        detail = protect_detail_lookup.get(hero.lower()) or {}
        reasons = []
        if hero in _clean_heroes(visuals.get("target_comp"), 6):
            reasons.append("preserves our projected comp route")
        if detail.get("enemy_ban_pressure"):
            reasons.append("denies their likely ban response")
        if detail.get("model_hint"):
            reasons.append(
                f"{detail.get('model_hint')} (score {detail.get('protect_score')}, net {detail.get('strategic_net_value')})"
            )
        if not reasons:
            reasons.append("keeps a high-value route piece open")
        next_protect_rows.append({
            "hero": hero,
            "detail": detail,
            "reasons": reasons[:3],
        })

    possible_pivots = []
    for row in pivot_rows[:4]:
        base = _clean_heroes(row.get("base"), 6)
        pivot = _clean_heroes(row.get("pivot"), 6)
        counter = _clean_heroes(row.get("counter"), 6)
        if not base or not pivot:
            continue
        blocked_base = [hero for hero in base if hero.lower() in locked]
        pressure_bans = [hero for hero in candidate_bans if hero in base or hero in pivot]
        possible_pivots.append({
            "base": base,
            "pivot": pivot,
            "counter": counter,
            "blocked_base": blocked_base,
            "pressure_bans": pressure_bans[:3],
            "diff_count": row.get("diff_count", 0),
        })

    def _resolve_next_action_type() -> str:
        explicit_action = str(current_phase.get("next_action_type") or "").strip().lower()
        if explicit_action in {"ban", "protect"}:
            if next_team == "a":
                return "our_protect" if explicit_action == "protect" else "our_ban"
            if next_team == "b":
                return "enemy_protect" if explicit_action == "protect" else "enemy_ban"

        next_slot = str(current_phase.get("next_slot") or "").strip().lower()
        if next_slot.startswith("protect"):
            if next_team == "a":
                return "our_protect"
            if next_team == "b":
                return "enemy_protect"
        if next_slot.startswith("ban"):
            if next_team == "a":
                return "our_ban"
            if next_team == "b":
                return "enemy_ban"

        phase_label_lc = str(current_phase.get("phase_label") or "").strip().lower()
        if "protect" in phase_label_lc:
            if next_team == "a":
                return "our_protect"
            if next_team == "b":
                return "enemy_protect"
        if "ban" in phase_label_lc:
            if next_team == "a":
                return "our_ban"
            if next_team == "b":
                return "enemy_ban"

        if next_team == "a":
            if len(our_locked_bans) in {1, 3} and len(our_locked_protects) < (1 if len(our_locked_bans) == 1 else 2):
                return "our_protect"
            return "our_ban"
        if next_team == "b":
            if len(enemy_locked_bans) in {1, 3} and len(enemy_locked_protects) < (1 if len(enemy_locked_bans) == 1 else 2):
                return "enemy_protect"
            return "enemy_ban"
        return "unknown"

    next_action_type = _resolve_next_action_type()

    enemy_remaining_bans = max(0, 4 - len(enemy_locked_bans))

    if next_action_type == "enemy_protect" and enemy_next_protects:
        predicted_protect_keys = {
            _canonical_draft_hero(str(hero or "")).lower()
            for hero in enemy_next_protects
            if str(hero or "").strip()
        }
        filtered_enemy_bans = [
            hero for hero in enemy_next_bans
            if _canonical_draft_hero(str(hero or "")).lower() not in predicted_protect_keys
        ]
        if filtered_enemy_bans:
            enemy_next_bans = filtered_enemy_bans
        filtered_ban_rows = [
            row for row in next_ban_rows
            if _canonical_draft_hero(str(row.get("hero") or "")).lower() not in predicted_protect_keys
        ]
        if filtered_ban_rows:
            next_ban_rows = filtered_ban_rows
        if enemy_remaining_bans <= 0:
            enemy_next_bans = []

    def _build_full_slot_plan(
        slot_keys: list[str],
        locked_values: list[str],
        candidate_values: list[str],
        slot_history_lookup: dict | None,
    ) -> dict[str, str]:
        plan: dict[str, str] = {}
        used_keys = set()
        slot_history_lookup = slot_history_lookup or {}

        for idx, slot_key in enumerate(slot_keys):
            if idx < len(locked_values):
                hero = _canonical_draft_hero(str(locked_values[idx] or "").strip())
                if hero:
                    plan[slot_key] = hero
                    used_keys.add(hero.lower())

        candidate_pool = _clean_heroes(candidate_values, 16)
        base_rank = {hero.lower(): idx for idx, hero in enumerate(candidate_pool)}

        for slot_key in slot_keys:
            if slot_key in plan:
                continue
            slot_scores = slot_history_lookup.get(slot_key) or {}
            best_hero = ""
            best_tuple = None
            for hero in candidate_pool:
                hero_key = hero.lower()
                if hero_key in used_keys:
                    continue
                score_tuple = (
                    float(slot_scores.get(hero_key, 0) or 0),
                    -int(base_rank.get(hero_key, 999)),
                )
                if best_tuple is None or score_tuple > best_tuple:
                    best_tuple = score_tuple
                    best_hero = hero
            if best_hero:
                plan[slot_key] = best_hero
                used_keys.add(best_hero.lower())
            else:
                plan[slot_key] = ""

        return plan

    def _format_slot_plan(plan: dict[str, str], slot_keys: list[str]) -> str:
        return " | ".join(
            f"{slot} {(plan.get(slot) or '—')}"
            for slot in slot_keys
        )

    def _pair_keys(values: list[str]) -> set[tuple[str, str]]:
        cleaned = _clean_heroes(values, 8)
        pairs: set[tuple[str, str]] = set()
        for left_idx in range(len(cleaned)):
            for right_idx in range(left_idx + 1, len(cleaned)):
                left = cleaned[left_idx]
                right = cleaned[right_idx]
                if left and right:
                    pairs.add(tuple(sorted((left.lower(), right.lower()))))
        return pairs

    def _map_mode_for_entry(map_entry: dict) -> str:
        mode_name = str(map_entry.get("mode") or map_entry.get("map_mode") or "").strip().lower()
        if mode_name:
            return mode_name
        map_name = str(map_entry.get("map_name") or map_entry.get("map") or "").strip()
        return str((MAP_MODES or {}).get(map_name, "") or "").strip().lower()

    def _draft_prefix_values(draft_row: dict, slot_type: str, count: int) -> list[str]:
        if not isinstance(draft_row, dict) or count <= 0:
            return []
        slot_order = ["ban1", "ban2", "ban3", "ban4"] if slot_type == "ban" else ["protect1", "protect2"]
        values: list[str] = []
        for slot_key in slot_order[:count]:
            hero_name = _canonical_draft_hero(str(draft_row.get(slot_key, "") or "").strip())
            if hero_name:
                values.append(hero_name)
        return values

    active_enemy_action = (
        "enemy_protect"
        if len(enemy_locked_bans) in {1, 3} and len(enemy_locked_protects) < (1 if len(enemy_locked_bans) == 1 else 2)
        else "enemy_ban"
    )

    def _build_similar_draft_snapshot() -> dict:
        team_b_id = chat_context.get("team_b_id")
        team_a_name = str(chat_context.get("team_a_name") or "").strip()
        selected_map = str(chat_context.get("map") or "all").strip()
        selected_mode = str(chat_context.get("mode_type") or "all").strip().lower()
        next_slot_key = next_enemy_protect_slot_key if active_enemy_action == "enemy_protect" else next_enemy_ban_slot_key
        if not team_b_id or not next_slot_key:
            return {"max_score": 0, "matches": [], "responses": []}

        team_b_row = get_db().execute("SELECT * FROM teams WHERE id = ?", (team_b_id,)).fetchone()
        if team_b_row is None:
            return {"max_score": 0, "matches": [], "responses": []}

        history = _machine_chat_filter_team_history(
            team_b_row,
            str(chat_context.get("season") or "all"),
            selected_map,
            bool(chat_context.get("include_scrims", True)),
            bool(chat_context.get("include_tournaments", True)),
            str(chat_context.get("map_type") or "all"),
            selected_mode,
        )

        current_enemy_team_heroes = _clean_heroes(enemy_locked_bans + enemy_locked_protects, 6)
        current_enemy_pairs = _pair_keys(current_enemy_team_heroes)
        current_ban_values = _clean_heroes(our_locked_bans + enemy_locked_bans, 8)
        current_ban_keys = {hero.lower() for hero in current_ban_values}
        blocked_response_keys = {
            hero.lower()
            for hero in _clean_heroes(
                our_locked_bans + enemy_locked_bans + our_locked_protects + enemy_locked_protects,
                12,
            )
        }
        max_score = (
            (len(current_enemy_team_heroes) * 3)
            + (len(current_enemy_pairs) * 2)
            + (2 if selected_map and selected_map.lower() != "all" else 0)
            + (1 if selected_mode and selected_mode != "all" else 0)
            + (1 if team_a_name else 0)
            + len(current_ban_values)
        )

        similar_matches: list[dict] = []
        response_summary: dict[str, dict] = {}

        for record in history:
            record_opponent = str(record.get("enemy_team") or record.get("opponent") or "").strip()
            for map_entry in record.get("maps", []) or []:
                if not isinstance(map_entry, dict):
                    continue
                team_slot = map_entry.get("our_team_slot", "team1")
                if team_slot not in TEAM_SLOTS:
                    team_slot = "team1"
                opponent_slot = _machine_agent_opposite_slot(team_slot)
                draft = map_entry.get("draft", {})
                enemy_draft = draft.get(team_slot, {}) if isinstance(draft, dict) else {}
                opponent_draft = draft.get(opponent_slot, {}) if isinstance(draft, dict) else {}
                if not isinstance(enemy_draft, dict) or not isinstance(opponent_draft, dict):
                    continue

                historical_enemy_team_heroes = _clean_heroes(
                    _draft_prefix_values(enemy_draft, "ban", len(enemy_locked_bans))
                    + _draft_prefix_values(enemy_draft, "protect", len(enemy_locked_protects)),
                    6,
                )
                historical_enemy_pairs = _pair_keys(historical_enemy_team_heroes)
                historical_ban_keys = {
                    hero.lower()
                    for hero in _clean_heroes(
                        _draft_prefix_values(enemy_draft, "ban", len(enemy_locked_bans))
                        + _draft_prefix_values(opponent_draft, "ban", len(our_locked_bans)),
                        8,
                    )
                }
                response_hero = _canonical_draft_hero(str(enemy_draft.get(next_slot_key, "") or "").strip())
                if not response_hero:
                    continue
                if response_hero.lower() in blocked_response_keys:
                    continue

                same_hero_count = len({hero.lower() for hero in current_enemy_team_heroes} & {hero.lower() for hero in historical_enemy_team_heroes})
                same_pair_count = len(current_enemy_pairs & historical_enemy_pairs)
                same_ban_count = len(current_ban_keys & historical_ban_keys)
                map_name = str(map_entry.get("map_name") or map_entry.get("map") or "").strip()
                map_mode = _map_mode_for_entry(map_entry)
                similarity_score = (same_hero_count * 3) + (same_pair_count * 2) + same_ban_count
                if selected_map and selected_map.lower() != "all" and map_name.lower() == selected_map.lower():
                    similarity_score += 2
                if selected_mode and selected_mode != "all" and map_mode == selected_mode:
                    similarity_score += 1
                if team_a_name and record_opponent and record_opponent.lower() == team_a_name.lower():
                    similarity_score += 1

                result = get_map_outcome_for_slot(map_entry, team_slot)
                match_row = {
                    "map_name": map_name or "Unknown Map",
                    "mode": map_mode or "unknown",
                    "response": response_hero,
                    "result": result or "Unknown",
                    "score": similarity_score,
                    "max_score": max_score,
                    "opponent": record_opponent or "Unknown Opponent",
                    "same_heroes": same_hero_count,
                    "same_pairs": same_pair_count,
                    "same_bans": same_ban_count,
                    "same_map": bool(selected_map and selected_map.lower() != "all" and map_name.lower() == selected_map.lower()),
                    "same_mode": bool(selected_mode and selected_mode != "all" and map_mode == selected_mode),
                    "same_enemy_team": bool(team_a_name and record_opponent and record_opponent.lower() == team_a_name.lower()),
                }
                similar_matches.append(match_row)

                response_bucket = response_summary.setdefault(
                    response_hero,
                    {
                        "hero": response_hero,
                        "count": 0,
                        "wins": 0,
                        "losses": 0,
                        "unknown": 0,
                        "total_similarity": 0.0,
                    },
                )
                response_bucket["count"] += 1
                response_bucket["total_similarity"] += float(similarity_score or 0)
                if result == "Win":
                    response_bucket["wins"] += 1
                elif result == "Loss":
                    response_bucket["losses"] += 1
                else:
                    response_bucket["unknown"] += 1

        similar_matches.sort(
            key=lambda row: (
                int(row.get("score", 0) or 0),
                1 if str(row.get("result") or "") == "Win" else 0,
                str(row.get("response") or ""),
            ),
            reverse=True,
        )
        response_rows = []
        for row in response_summary.values():
            decided = int(row.get("wins", 0) or 0) + int(row.get("losses", 0) or 0)
            avg_similarity = (float(row.get("total_similarity", 0) or 0) / int(row.get("count", 0) or 1)) if row.get("count") else 0.0
            win_rate = ((int(row.get("wins", 0) or 0) / decided) * 100.0) if decided else 0.0
            row["decided"] = decided
            row["avg_similarity"] = round(avg_similarity, 1)
            row["win_rate"] = round(win_rate, 1)
            row["effectiveness"] = round(float(row.get("total_similarity", 0) or 0) * (win_rate / 100.0), 2)
            response_rows.append(row)
        response_rows.sort(
            key=lambda row: (
                float(row.get("effectiveness", 0) or 0),
                int(row.get("count", 0) or 0),
                float(row.get("avg_similarity", 0) or 0),
                str(row.get("hero") or ""),
            ),
            reverse=True,
        )
        return {
            "max_score": max_score,
            "matches": similar_matches[:5],
            "responses": response_rows[:5],
        }

    our_ban_plan = _build_full_slot_plan(
        ["ban1", "ban2", "ban3", "ban4"],
        our_locked_bans,
        [row.get("hero", "") for row in next_ban_rows if row.get("hero")],
        visuals.get("our_ban_slot_history") or {},
    )
    our_protect_plan = _build_full_slot_plan(
        ["protect1", "protect2"],
        our_locked_protects,
        [row.get("hero", "") for row in next_protect_rows if row.get("hero")],
        visuals.get("our_protect_slot_history") or {},
    )
    enemy_ban_plan = _build_full_slot_plan(
        ["ban1", "ban2", "ban3", "ban4"],
        enemy_locked_bans,
        enemy_next_bans,
        visuals.get("enemy_ban_slot_history") or {},
    )
    enemy_protect_plan = _build_full_slot_plan(
        ["protect1", "protect2"],
        enemy_locked_protects,
        enemy_next_protects,
        visuals.get("enemy_protect_slot_history") or {},
    )
    similar_draft_snapshot = _build_similar_draft_snapshot()

    lines = ["Live decision packet:"]
    if next_action_type == "our_ban":
        lines.append("- It is our ban turn; prioritize the highest-impact removal on enemy route continuity.")
    elif next_action_type == "our_protect":
        lines.append("- It is our protect turn; preserve our route piece before the next enemy ban.")
    elif next_action_type == "enemy_ban":
        lines.append("- Enemy ban turn is next; predict which of our route pieces they remove.")
    elif next_action_type == "enemy_protect":
        lines.append("- Enemy protect turn is next; predict which route anchor they lock before banning.")
    if next_ban_rows:
        lines.append("- Next ban candidates:")
        for row in next_ban_rows[:3]:
            lines.append(f"  - {row['hero']}: " + "; ".join(row["reasons"]))
        lines.append(
            "- Ranked bans (do not reorder): "
            + " | ".join(
                f"#{idx + 1} {row.get('hero', '')}"
                for idx, row in enumerate(next_ban_rows[:4])
                if row.get("hero")
            )
        )
    if next_protect_rows:
        lines.append("- Next protect candidates:")
        for row in next_protect_rows[:3]:
            lines.append(f"  - {row['hero']}: " + "; ".join(row["reasons"]))
        lines.append(
            "- Ranked protects (do not reorder): "
            + " | ".join(
                f"#{idx + 1} {row.get('hero', '')}"
                for idx, row in enumerate(next_protect_rows[:3])
                if row.get("hero")
            )
        )
    if enemy_next_bans:
        lines.append("- Enemy likely next bans: " + _machine_chat_join(enemy_next_bans, 4))
    if similar_draft_snapshot.get("matches"):
        lines.append("- Similar past drafts (scored):")
        for row in (similar_draft_snapshot.get("matches") or [])[:3]:
            lines.append(
                f"  - {row.get('map_name') or 'Unknown Map'} | similarity {int(row.get('score', 0) or 0)}/{int(similar_draft_snapshot.get('max_score') or 0)}"
                f" | enemy response {row.get('response') or 'Unknown'} | result {row.get('result') or 'Unknown'}"
            )
    if similar_draft_snapshot.get("responses"):
        lines.append("- Historical response results from top similar drafts:")
        for row in (similar_draft_snapshot.get("responses") or [])[:3]:
            lines.append(
                f"  - {row.get('hero') or 'Unknown'}: {int(row.get('wins', 0) or 0)}W-{int(row.get('losses', 0) or 0)}L"
                f" across {int(row.get('count', 0) or 0)} similar drafts"
                f" | avg similarity {float(row.get('avg_similarity', 0) or 0):.1f}/{int(similar_draft_snapshot.get('max_score') or 0)}"
            )
    lines.append("- Historical best-in-slot (our bans): " + _format_slot_plan(our_ban_plan, ["ban1", "ban2", "ban3", "ban4"]))
    lines.append("- Historical best-in-slot (our protects): " + _format_slot_plan(our_protect_plan, ["protect1", "protect2"]))
    lines.append("- Historical best-in-slot (enemy bans): " + _format_slot_plan(enemy_ban_plan, ["ban1", "ban2", "ban3", "ban4"]))
    lines.append("- Historical best-in-slot (enemy protects): " + _format_slot_plan(enemy_protect_plan, ["protect1", "protect2"]))
    if possible_pivots and draft_complete:
        lines.append("- Possible pivot paths:")
        for row in possible_pivots[:3]:
            pressure = _machine_chat_join(row.get("pressure_bans", []), 3)
            pressure_text = f"; pressure bans: {pressure}" if pressure else ""
            counter = _machine_chat_join(row.get("counter", []), 3)
            counter_text = f"; our answer: {counter}" if counter else ""
            lines.append(
                f"  - {team_b} {_machine_chat_join(row['base'], 4)}"
                f" -> {_machine_chat_join(row['pivot'], 4)}"
                f" ({row.get('diff_count', 0)} hero shift{pressure_text}{counter_text})"
            )
    lines.append(
        "You are a Marvel Rivals counter-draft AI. You are acting as the enemy team's draft coach, but you may ONLY use historical data. "
        "Do not use matchup theory. Do not use hero theory. Do not infer counters unless they appear in the data. "
        "Do not require exact matches. Use the closest fuzzy historical matches available. "
        "Use only past drafts, past bans, past picks, past map results, past team-specific tendencies, past hero pairings, past win/loss outcomes, and past responses to similar drafts. "
        "Calculate similar drafts with this exact scoring: +3 for each same hero on your team, +2 for each same hero pair, +2 for same map, +1 for same mode, +1 for same enemy team, +1 for each same ban. Pull the top matches. "
        "Never say: No exact match found, so no recommendation. Instead say: Closest historical counter found with Low, Medium, or High confidence. "
        "Output exactly in this format: Closest historical match count, Average similarity score, Shared features, Most common enemy response, Best-performing enemy response, Projected enemy counter move, Historical reasoning, Historical record, Sample size, Confidence, Missing data. "
        "Only make recommendations if they are directly supported by historical draft data. Use only provided historical/model rows; do not invent heroes, stats, or draft events."
    )

    return "\n".join(lines), {
        "next_bans": next_ban_rows,
        "next_protects": next_protect_rows,
        "enemy_next_protects": enemy_next_protects[:4],
        "enemy_next_bans": enemy_next_bans[:4],
        "enemy_next_ban_slot_key": next_enemy_ban_slot_key,
        "enemy_next_protect_slot_key": next_enemy_protect_slot_key,
        "possible_pivots": possible_pivots if draft_complete else [],
        "our_projected_path": _clean_heroes(visuals.get("target_comp"), 6) if draft_complete else [],
        "their_projected_path": _clean_heroes((enemy_comp_rows[0] or {}).get("heroes"), 6) if draft_complete and enemy_comp_rows else [],
        "their_projected_rate": (enemy_comp_rows[0] or {}).get("rate", 0) if draft_complete and enemy_comp_rows else 0,
        "their_projected_wr": (enemy_comp_rows[0] or {}).get("win_rate", 0) if draft_complete and enemy_comp_rows else 0,
        "team_a": team_a,
        "team_b": team_b,
        "current_phase": current_phase,
        "draft_complete": draft_complete,
        "next_action_type": next_action_type,
        "enemy_response_action_type": (
            "enemy_protect"
            if len(enemy_locked_bans) in {1, 3} and len(enemy_locked_protects) < (1 if len(enemy_locked_bans) == 1 else 2)
            else "enemy_ban"
        ),
        "similar_draft_matches": similar_draft_snapshot.get("matches") or [],
        "similar_draft_responses": similar_draft_snapshot.get("responses") or [],
        "similar_draft_max_score": int(similar_draft_snapshot.get("max_score") or 0),
        "our_locked_bans": our_locked_bans,
        "enemy_locked_bans": enemy_locked_bans,
        "our_locked_protects": our_locked_protects,
        "enemy_locked_protects": enemy_locked_protects,
        "our_ban_plan": our_ban_plan,
        "our_protect_plan": our_protect_plan,
        "enemy_ban_plan": enemy_ban_plan,
        "enemy_protect_plan": enemy_protect_plan,
    }


def _machine_agent_live_decision_fallback(decision_data: dict | None) -> str:
    if not decision_data:
        return ""
    next_bans = decision_data.get("next_bans") or []
    next_protects = decision_data.get("next_protects") or []
    enemy_next_protects = decision_data.get("enemy_next_protects") or []
    enemy_next_bans = decision_data.get("enemy_next_bans") or []
    pivots = decision_data.get("possible_pivots") or []
    our_path = decision_data.get("our_projected_path") or []
    their_path = decision_data.get("their_projected_path") or []
    their_rate = decision_data.get("their_projected_rate") or 0
    their_wr = decision_data.get("their_projected_wr") or 0
    our_locked_bans = decision_data.get("our_locked_bans") or []
    enemy_locked_bans = decision_data.get("enemy_locked_bans") or []
    our_locked_protects = decision_data.get("our_locked_protects") or []
    enemy_locked_protects = decision_data.get("enemy_locked_protects") or []
    our_ban_plan = decision_data.get("our_ban_plan") or {}
    our_protect_plan = decision_data.get("our_protect_plan") or {}
    enemy_ban_plan = decision_data.get("enemy_ban_plan") or {}
    enemy_protect_plan = decision_data.get("enemy_protect_plan") or {}
    enemy_next_ban_slot_key = str(decision_data.get("enemy_next_ban_slot_key") or "").strip().lower()
    enemy_next_protect_slot_key = str(decision_data.get("enemy_next_protect_slot_key") or "").strip().lower()
    enemy_response_action_type = str(decision_data.get("enemy_response_action_type") or "").strip().lower()
    similar_draft_matches = decision_data.get("similar_draft_matches") or []
    similar_draft_responses = decision_data.get("similar_draft_responses") or []
    similar_draft_max_score = int(decision_data.get("similar_draft_max_score") or 0)
    draft_complete = bool(decision_data.get("draft_complete"))
    if not next_bans and not next_protects and not pivots and not enemy_next_bans:
        return ""

    def _enemy_response_reason(hero: str, action_type: str) -> str:
        clean_hero = _canonical_draft_hero(str(hero or "").strip())
        if not clean_hero:
            return "No clean historical trigger yet."
        if action_type == "enemy_protect":
            slot_key = enemy_next_protect_slot_key or "protect1"
            return f"Historical {slot_key} data makes this their most common next protect."
        slot_key = enemy_next_ban_slot_key or "ban1"
        return f"Historical {slot_key} data makes this their most common next ban."

    def _enemy_response_confidence(pool: list[str], hero: str) -> str:
        if not hero:
            return "Low"
        if pool and len(pool) > 1 and pool[0] == hero:
            return "High"
        if pool:
            return "Med"
        return "Low"

    active_enemy_action = enemy_response_action_type or (
        "enemy_protect"
        if len(enemy_locked_bans) in {1, 3} and len(enemy_locked_protects) < (1 if len(enemy_locked_bans) == 1 else 2)
        else "enemy_ban"
    )
    active_enemy_pool = enemy_next_protects if active_enemy_action == "enemy_protect" else enemy_next_bans

    most_likely_hero = ""
    most_likely_reason = "No clean historical trigger yet."
    most_likely_confidence = "Low"
    if active_enemy_pool:
        most_likely_hero = active_enemy_pool[0]
        most_likely_reason = _enemy_response_reason(most_likely_hero, active_enemy_action)
        most_likely_confidence = _enemy_response_confidence(active_enemy_pool, most_likely_hero)
    elif their_path:
        most_likely_hero = their_path[0]
        most_likely_reason = "This is the clearest remaining route anchor in their historical path data."

    dangerous_hero = most_likely_hero or (active_enemy_pool[0] if active_enemy_pool else "Unknown")
    dangerous_reason = _enemy_response_reason(dangerous_hero, active_enemy_action)
    dangerous_confidence = _enemy_response_confidence(active_enemy_pool, dangerous_hero)

    best_historical_response = similar_draft_responses[0] if similar_draft_responses else {}
    if best_historical_response.get("hero"):
        recommended_enemy_move = str(best_historical_response.get("hero") or "").strip()
    else:
        recommended_enemy_move = dangerous_hero if dangerous_hero and dangerous_hero != "Unknown" else most_likely_hero

    our_adjustment = "pivot"
    if next_protects:
        our_adjustment = f"protect {next_protects[0].get('hero') or ''}".strip()
    elif next_bans:
        our_adjustment = f"ban {next_bans[0].get('hero') or ''}".strip()

    response_action_label = "pick" if active_enemy_action == "enemy_protect" else "ban"
    similar_match_count = len(similar_draft_matches)
    avg_similarity = (
        sum(float(row.get("score", 0) or 0) for row in similar_draft_matches) / similar_match_count
        if similar_match_count else 0.0
    )
    top_match = similar_draft_matches[0] if similar_draft_matches else {}
    shared_features: list[str] = []
    if top_match:
        same_heroes = int(top_match.get("same_heroes", 0) or 0)
        same_pairs = int(top_match.get("same_pairs", 0) or 0)
        same_bans = int(top_match.get("same_bans", 0) or 0)
        if same_heroes:
            shared_features.append(f"- same heroes: {same_heroes}")
        if same_pairs:
            shared_features.append(f"- same hero pairs: {same_pairs}")
        if same_bans:
            shared_features.append(f"- same bans: {same_bans}")
        if top_match.get("same_map"):
            shared_features.append(f"- same map: {top_match.get('map_name') or 'Unknown Map'}")
        if top_match.get("same_mode"):
            shared_features.append(f"- same mode: {top_match.get('mode') or 'unknown'}")
        if top_match.get("same_enemy_team"):
            shared_features.append(f"- same enemy team: {top_match.get('opponent') or 'Unknown Opponent'}")
    if not shared_features:
        shared_features.append("- low-overlap fuzzy match only")

    best_count = int(best_historical_response.get("count", 0) or 0)
    best_win_rate = float(best_historical_response.get("win_rate", 0) or 0)
    best_avg_similarity = float(best_historical_response.get("avg_similarity", 0) or 0)
    max_ratio = (best_avg_similarity / similar_draft_max_score) if similar_draft_max_score > 0 else 0.0
    if best_count >= 3 and best_win_rate >= 55 and max_ratio >= 0.55:
        confidence = "High"
    elif best_count >= 2 and best_win_rate >= 50 and max_ratio >= 0.4:
        confidence = "Medium"
    elif recommended_enemy_move:
        confidence = dangerous_confidence or most_likely_confidence or "Low"
    else:
        confidence = "Low"
    if confidence == "Med":
        confidence = "Medium"

    most_common_response = {}
    if similar_draft_responses:
        most_common_response = sorted(
            similar_draft_responses,
            key=lambda row: (
                int(row.get("count", 0) or 0),
                float(row.get("avg_similarity", 0) or 0),
                str(row.get("hero") or ""),
            ),
            reverse=True,
        )[0]

    best_record = f"{int(best_historical_response.get('wins', 0) or 0)}W-{int(best_historical_response.get('losses', 0) or 0)}L" if best_count else "Not enough historical evidence."
    sample_size_text = f"{best_count} similar drafts" if best_count else (f"{similar_match_count} fuzzy matches" if similar_match_count else "0 similar drafts")
    avg_similarity_text = f"{avg_similarity:.1f}/{similar_draft_max_score}" if similar_match_count and similar_draft_max_score else "0.0/0"
    most_common_response_text = (
        f"{most_common_response.get('hero') or 'Unknown'} {response_action_label} in {int(most_common_response.get('count', 0) or 0)} similar drafts"
        if most_common_response else
        (f"{most_likely_hero} {response_action_label}" if most_likely_hero else "Closest historical counter found with Low confidence.")
    )
    best_performing_response_text = (
        f"{best_historical_response.get('hero') or 'Unknown'} {response_action_label} | {best_record} | avg similarity {best_avg_similarity:.1f}/{similar_draft_max_score or 1}"
        if best_count else
        (f"{recommended_enemy_move} {response_action_label}" if recommended_enemy_move else "Closest historical counter found with Low confidence.")
    )
    reasoning_lines: list[str] = []
    if recommended_enemy_move:
        reasoning_lines.append(
            f"Projected from {sample_size_text} where {recommended_enemy_move} was the strongest historical {response_action_label} response."
        )
    if top_match:
        match_bits: list[str] = []
        if int(top_match.get("same_heroes", 0) or 0):
            match_bits.append(f"{int(top_match.get('same_heroes', 0) or 0)} same heroes")
        if int(top_match.get("same_pairs", 0) or 0):
            match_bits.append(f"{int(top_match.get('same_pairs', 0) or 0)} same hero pairs")
        if int(top_match.get("same_bans", 0) or 0):
            match_bits.append(f"{int(top_match.get('same_bans', 0) or 0)} same bans")
        if top_match.get("same_map"):
            match_bits.append("same map")
        if top_match.get("same_mode"):
            match_bits.append("same mode")
        if top_match.get("same_enemy_team"):
            match_bits.append("same enemy team")
        if match_bits:
            reasoning_lines.append(
                "Closest matching draft features: " + ", ".join(match_bits) + "."
            )
    if best_count:
        reasoning_lines.append(
            f"Historical result for this response: {best_record} with average similarity {best_avg_similarity:.1f}/{similar_draft_max_score or 1}."
        )
    elif most_likely_reason:
        reasoning_lines.append(most_likely_reason)
    if not reasoning_lines:
        reasoning_lines.append("Not enough historical evidence.")
    missing_data_parts: list[str] = []
    if similar_match_count < 3:
        missing_data_parts.append("limited close fuzzy matches")
    if best_count < 3:
        missing_data_parts.append("small best-response sample")
    if not similar_draft_max_score:
        missing_data_parts.append("no strong shared map/mode/team markers")
    if not missing_data_parts:
        missing_data_parts.append("none significant in current filter")
    missing_data_text = ", ".join(missing_data_parts)

    return "\n".join([
        f"Closest historical counter found with {confidence} confidence.",
        "Closest historical match count:",
        str(similar_match_count),
        "",
        "Average similarity score:",
        avg_similarity_text,
        "",
        "Shared features:",
        *shared_features,
        "",
        "Most common enemy response:",
        most_common_response_text,
        "",
        "Best-performing enemy response:",
        best_performing_response_text,
        "",
        "Projected enemy counter move:",
        f"{recommended_enemy_move} {response_action_label}" if recommended_enemy_move else f"Closest historical counter found with {confidence} confidence.",
        "",
        "Historical reasoning:",
        *reasoning_lines,
        "",
        "Historical record:",
        best_record,
        "",
        "Sample size:",
        sample_size_text,
        "",
        "Confidence:",
        confidence,
        "",
        "Missing data:",
        missing_data_text,
    ]).strip()

    explained_pivot = ""
    top = next_bans[0] if next_bans else {}
    top_protect = next_protects[0] if next_protects else {}
    top_hero = top.get("hero") or "their highest-value remaining route piece"
    top_protect_hero = top_protect.get("hero") or ""
    top_enemy_ban = enemy_next_bans[0] if enemy_next_bans else ""
    remaining_bans = max(0, 4 - len(our_locked_bans))
    remaining_protects = max(0, 2 - len(our_locked_protects))
    enemy_remaining_bans = max(0, 4 - len(enemy_locked_bans))
    next_ban_number = min(len(our_locked_bans) + 1, 4)
    next_protect_number = min(len(our_locked_protects) + 1, 2)

    lines = ["Live draft read:"]
    if our_locked_bans:
        lines.append(f"We are banning: {_machine_chat_join(our_locked_bans, 4)}.")
    else:
        lines.append("We are banning first from a clean board.")
    current_phase = decision_data.get("current_phase") or {}
    phase_label_lc = str(current_phase.get("phase_label") or "").lower()
    next_team = current_phase.get("next_team")
    next_action_type = str(decision_data.get("next_action_type") or "").strip().lower()
    if not next_action_type:
        explicit_action = str(current_phase.get("next_action_type") or "").strip().lower()
        if explicit_action in {"ban", "protect"}:
            if next_team == "a":
                next_action_type = "our_protect" if explicit_action == "protect" else "our_ban"
            elif next_team == "b":
                next_action_type = "enemy_protect" if explicit_action == "protect" else "enemy_ban"
        if not next_action_type:
            next_slot = str(current_phase.get("next_slot") or "").strip().lower()
            if next_slot.startswith("protect"):
                next_action_type = "our_protect" if next_team == "a" else ("enemy_protect" if next_team == "b" else "unknown")
            elif next_slot.startswith("ban"):
                next_action_type = "our_ban" if next_team == "a" else ("enemy_ban" if next_team == "b" else "unknown")
    if not next_action_type:
        if "protect" in phase_label_lc:
            next_action_type = "our_protect" if next_team == "a" else ("enemy_protect" if next_team == "b" else "unknown")
        elif "ban" in phase_label_lc:
            next_action_type = "our_ban" if next_team == "a" else ("enemy_ban" if next_team == "b" else "unknown")
        elif next_team == "a":
            next_action_type = "our_protect" if len(our_locked_bans) in {1, 3} and len(our_locked_protects) < 2 else "our_ban"
        elif next_team == "b":
            next_action_type = "enemy_protect" if len(enemy_locked_bans) in {1, 3} and len(enemy_locked_protects) < 2 else "enemy_ban"
        else:
            next_action_type = "unknown"

    open_protect_turn = next_action_type == "our_protect"
    enemy_protect_turn = next_action_type == "enemy_protect"
    if open_protect_turn and not top_protect_hero:
        locked_names = {
            str(h or "").strip().lower()
            for h in (our_locked_bans + enemy_locked_bans + our_locked_protects + enemy_locked_protects)
        }
        top_protect_hero = next(
            (hero for hero in (our_path or []) if str(hero or "").strip().lower() not in locked_names),
            "",
        )
    if enemy_protect_turn:
        enemy_next_protect = enemy_next_protects[0] if enemy_next_protects else ""
        predicted_protect_keys = {
            _canonical_draft_hero(str(hero or "")).lower()
            for hero in enemy_next_protects
            if str(hero or "").strip()
        }
        enemy_next_bans = [
            hero for hero in enemy_next_bans
            if _canonical_draft_hero(str(hero or "")).lower() not in predicted_protect_keys
        ]
        if enemy_remaining_bans <= 0:
            enemy_next_bans = []
        if enemy_next_protect:
            lines.append(
                f"Based off historical data, their likely protect for the comp they want to play is: {enemy_next_protect}."
            )
        elif enemy_locked_protects:
            lines.append(f"Their protect line so far: {_machine_chat_join(enemy_locked_protects, 2)}.")
        else:
            lines.append("They should protect next: unclear.")
        if top_enemy_ban and enemy_next_bans:
            top_enemy_ban = enemy_next_bans[0]
            lines.append(
                f"Based off historical data, their best ban after protect for the comp they want to play is: {top_enemy_ban}."
            )
    else:
        if top_enemy_ban:
            lines.append(
                f"Based off historical data, their best next ban for the comp they want to play is: {top_enemy_ban}."
            )
        elif enemy_locked_bans:
            lines.append(f"Their ban line so far: {_machine_chat_join(enemy_locked_bans, 4)}.")
    if open_protect_turn:
        lines.append("Action type: Protect turn.")
    elif enemy_protect_turn:
        lines.append("Action type: Enemy protect turn.")
    elif next_action_type == "our_ban":
        lines.append("Action type: Ban turn.")
    elif next_action_type == "enemy_ban":
        lines.append("Action type: Enemy ban turn.")
    if open_protect_turn and remaining_protects <= 0:
        lines.append("We have no protects remaining (max 2).")
    elif (enemy_protect_turn or next_action_type == "our_ban") and remaining_bans <= 0:
        lines.append("We have no bans remaining (max 4).")
    elif open_protect_turn:
        lines.append(f"We should protect: {top_protect_hero} (protect {next_protect_number}).")
    elif enemy_protect_turn:
        predicted_protect_keys = {
            _canonical_draft_hero(str(hero or "")).lower()
            for hero in enemy_next_protects
            if str(hero or "").strip()
        }
        if _canonical_draft_hero(str(top_hero or "")).lower() in predicted_protect_keys:
            replacement_row = next(
                (
                    row
                    for row in next_bans[1:]
                    if _canonical_draft_hero(str(row.get("hero") or "")).lower() not in predicted_protect_keys
                ),
                {},
            )
            if replacement_row:
                top = replacement_row
                top_hero = replacement_row.get("hero") or top_hero
        lines.append(f"We should ban after their protect: {top_hero} (ban {next_ban_number}).")
    else:
        lines.append(f"We should ban: {top_hero} (ban {next_ban_number}).")

    if draft_complete and (our_path or their_path):
        if their_path:
            metrics = []
            if their_rate:
                metrics.append(f"{float(their_rate):.1f}% path rate")
            if their_wr:
                metrics.append(f"{float(their_wr):.1f}% WR")
            metric_text = f" ({', '.join(metrics)})" if metrics else ""
            lines.append(f"Enemy comp: {_machine_chat_join(their_path, 6)}{metric_text}.")
        if our_path:
            lines.append(f"Our comp: {_machine_chat_join(our_path, 6)}.")
    elif not draft_complete:
        lines.append("Comp reveal: held until the draft phase is complete.")
    if our_locked_protects:
        lines.append(f"Our protects: {_machine_chat_join(our_locked_protects, 2)}.")
    if enemy_locked_protects:
        lines.append(f"Their protects: {_machine_chat_join(enemy_locked_protects, 2)}.")
    if our_ban_plan:
        lines.append(
            "Historical best-in-slot (our bans): "
            + " | ".join(f"{slot} {our_ban_plan.get(slot) or '—'}" for slot in ("ban1", "ban2", "ban3", "ban4"))
            + "."
        )
    if our_protect_plan:
        lines.append(
            "Historical best-in-slot (our protects): "
            + " | ".join(f"{slot} {our_protect_plan.get(slot) or '—'}" for slot in ("protect1", "protect2"))
            + "."
        )
    if enemy_ban_plan:
        lines.append(
            "Historical best-in-slot (enemy bans): "
            + " | ".join(f"{slot} {enemy_ban_plan.get(slot) or '—'}" for slot in ("ban1", "ban2", "ban3", "ban4"))
            + "."
        )
    if enemy_protect_plan:
        lines.append(
            "Historical best-in-slot (enemy protects): "
            + " | ".join(f"{slot} {enemy_protect_plan.get(slot) or '—'}" for slot in ("protect1", "protect2"))
            + "."
        )

    if next_bans and not open_protect_turn:
        reasons = list(top.get("reasons") or [])
        paths = top.get("pivot_paths") or []
        closes = top.get("closes") or []
        detail = top.get("detail") or {}
        if detail:
            role_label = detail.get("role") or detail.get("role_alias") or ""
            anova = detail.get("anova_significance") or {}
            anova_suffix = ""
            if anova:
                anova_suffix = (
                    f" | ANOVA {anova.get('significance', 'n/a')}"
                    f" F {anova.get('f_stat', 0)}"
                    f" lift {anova.get('loss_lift_pp', 0)}pp"
                )
            permutation = detail.get("permutation_importance") or {}
            permutation_suffix = ""
            if permutation:
                permutation_suffix = (
                    f" | permutation {permutation.get('importance', 'n/a')}"
                    f" rel {permutation.get('relative_lift', 0)}"
                )
            lines.append(
                "Candidate model: "
                f"{top_hero}"
                + (f" ({role_label})" if role_label else "")
                + f" | ban score {detail.get('ban_score', 0)}"
                + f" | net {detail.get('strategic_net_value', 0)}"
                + f" | enemy value {detail.get('strategic_enemy_value', 0)}"
                + f" | our cost {detail.get('strategic_our_value', 0)}"
                + f"{anova_suffix}"
                + f"{permutation_suffix}."
            )
        if closes:
            close = closes[0]
            metric_bits = []
            if close.get("rate"):
                metric_bits.append(f"{float(close.get('rate') or 0):.1f}% path rate")
            if close.get("win_rate"):
                metric_bits.append(f"{float(close.get('win_rate') or 0):.1f}% WR")
            if close.get("confidence"):
                metric_bits.append(f"{float(close.get('confidence') or 0):.1f}% confidence")
            if metric_bits:
                if draft_complete:
                    lines.append(f"Numerical reference: this touches their {_machine_chat_join(close.get('heroes', []), 4)} path ({', '.join(metric_bits)}).")
                else:
                    lines.append(f"Numerical reference: this touches an enemy route ({', '.join(metric_bits)}).")
                reasons = [
                    reason for reason in reasons
                    if not reason.startswith("hits enemy comp path ")
                ]
        if reasons:
            lines.append("Reason: " + "; ".join(reasons[:3]) + ".")
        if detail and detail.get("strategic_caution"):
            lines.append(f"Caution: {detail.get('strategic_caution')}.")
        if detail and detail.get("why_not_reason"):
            lines.append(f"Why not: {detail.get('why_not_reason')}.")
        if paths:
            path = paths[0]
            base_text = _machine_chat_join(path.get("from", []), 4)
            pivot_text = _machine_chat_join(path.get("to", []), 4)
            explained_pivot = pivot_text
            shift = int(path.get("diff_count") or 0)
            shift_text = f" ({shift}-hero shift)" if shift else ""
            if path.get("type") == "forces_pivot":
                if draft_complete:
                    lines.append(
                        f"Coach note: banning {top_hero} attacks their base shell ({base_text}) and likely pushes them toward {pivot_text}{shift_text}."
                    )
                else:
                    lines.append(f"Coach note: banning {top_hero} attacks their base shell; comp path withheld until draft completion.")
            else:
                if draft_complete:
                    lines.append(
                        f"Coach note: banning {top_hero} blocks their likely fallback into {pivot_text}{shift_text}; if they stay on {base_text}, that route is less flexible."
                    )
                else:
                    lines.append(f"Coach note: banning {top_hero} blocks a fallback route; comp path withheld until draft completion.")
    if open_protect_turn and top_protect:
        detail = top_protect.get("detail") or {}
        if detail:
            role_label = detail.get("role") or detail.get("role_alias") or ""
            lines.append(
                "Protect model: "
                f"{top_protect_hero}"
                + (f" ({role_label})" if role_label else "")
                + f" | protect score {detail.get('protect_score', 0)}"
                + f" | net {detail.get('strategic_net_value', 0)}"
                + f" | our value {detail.get('strategic_our_value', 0)}"
                + f" | enemy value {detail.get('strategic_enemy_value', 0)}."
            )
        reasons = list(top_protect.get("reasons") or [])
        if reasons:
            lines.append("Protect reason: " + "; ".join(reasons[:3]) + ".")
        if detail and detail.get("strategic_caution"):
            lines.append(f"Protect caution: {detail.get('strategic_caution')}.")
        if detail and detail.get("why_not_reason"):
            lines.append(f"Why not protect: {detail.get('why_not_reason')}.")
    if pivots and draft_complete:
        row = pivots[0]
        base_text = _machine_chat_join(row.get("base", []), 4)
        pivot_text = _machine_chat_join(row.get("pivot", []), 4)
        if pivot_text and pivot_text != explained_pivot:
            shift = int(row.get("diff_count") or 0)
            shift_text = f" ({shift}-hero shift)" if shift else ""
            lines.append(f"Expected fallback if their base is pressured: {pivot_text}{shift_text}.")
        elif base_text:
            lines.append(f"Current base shell to watch: {base_text}.")
        if row.get("counter"):
            lines.append(f"Our answer: {_machine_chat_join(row.get('counter', []), 4)}.")
    lines.append("Risk: if this does not remove a core route piece, they may still reach a clean fallback comp.")
    lines.append("Confidence: Medium unless this board has a repeated sample in the selected filters.")
    return "\n".join(lines)


def _machine_agent_hide_live_comps_until_complete(meta: dict, chat_context: dict) -> None:
    draft_live = chat_context.get("draft_live")
    if not isinstance(draft_live, dict) or not draft_live.get("active"):
        return
    if not isinstance(draft_live.get("current_phase"), dict):
        return
    visuals = meta.get("visuals")
    if not isinstance(visuals, dict):
        return

    for key in (
        "target_comp",
        "enemy_comps",
        "our_comp_rows",
        "pivot_predictions",
        "comp_tree",
        "likely_next_pick",
    ):
        visuals.pop(key, None)

    coach_read = visuals.get("coach_read")
    if isinstance(coach_read, dict):
        for key in ("expected_primary_comp", "expected_pivot", "our_answer"):
            coach_read.pop(key, None)

    confidence = visuals.get("confidence")
    if isinstance(confidence, dict):
        confidence.pop("target_comp", None)

    hero_focus = visuals.get("hero_focus")
    if isinstance(hero_focus, dict):
        hero_focus.pop("in_target_comp", None)

    live_decision = visuals.get("live_decision")
    if isinstance(live_decision, dict):
        live_decision["our_projected_path"] = []
        live_decision["their_projected_path"] = []
        live_decision["possible_pivots"] = []


def _machine_agent_live_draft_fallback(chat_context: dict) -> str:
    draft_live = chat_context.get("draft_live")
    if not isinstance(draft_live, dict) or not draft_live.get("active"):
        return ""

    team_a = chat_context.get("team_a_name") or "our team"
    team_b = chat_context.get("team_b_name") or "the enemy"
    our_bans = [str(v).strip() for v in (draft_live.get("our_bans") or []) if str(v).strip()]
    our_protects = [str(v).strip() for v in (draft_live.get("our_protects") or []) if str(v).strip()]
    enemy_bans = [str(v).strip() for v in (draft_live.get("enemy_bans") or []) if str(v).strip()]
    enemy_protects = [str(v).strip() for v in (draft_live.get("enemy_protects") or []) if str(v).strip()]
    current_phase = draft_live.get("current_phase") or {}
    next_team = current_phase.get("next_team")
    next_side = "our" if next_team == "a" else ("enemy" if next_team == "b" else "next")

    locked = our_bans + our_protects + enemy_bans + enemy_protects
    if not locked:
        return ""

    pieces = []
    if our_bans:
        pieces.append(f"Our bans are shaping the draft around {', '.join(our_bans[:4])}.")
    if our_protects:
        pieces.append(f"Our protected route is {', '.join(our_protects[:2])}.")
    if enemy_bans:
        pieces.append(f"{team_b} has already removed {', '.join(enemy_bans[:4])}, so treat those as pressure on our comfort route.")
    if enemy_protects:
        pieces.append(f"{team_b} protected {', '.join(enemy_protects[:2])}; that is the clearest enemy commitment right now.")

    next_action = "Next action: "
    if next_team == "a":
        if len(our_protects) < 2:
            next_action += "protect the hero that keeps the best remaining comp path alive, unless an enemy S-tier comfort pick is still open."
        else:
            next_action += "ban the strongest enemy comfort or the hero that completes their protected route."
    elif next_team == "b":
        next_action += "expect the enemy to answer by removing our most obvious comfort piece or protecting the hero their route depends on."
    else:
        next_action += "draft is mostly locked; evaluate whether the final bans closed the enemy route or only traded comfort."

    return (
        f"Live read: {team_a} vs {team_b}.\n\n"
        + "\n".join(pieces)
        + f"\n\nCurrent turn is {next_side}. Biggest danger is over-banning names without closing a comp path. "
        + next_action
    )


def _machine_agent_missing_context_response(intent: str, context: dict) -> str | None:
    if context.get("team_a_id") and context.get("team_b_id"):
        return None
    if not context.get("team_b_id"):
        if intent == "map":
            return "Which team are we drafting against, and what season or patch should I use? Example: `against Liquid Citadel season 7`."
        return "Who is the enemy team? Example: `against Virtus Pro season 7`."
    if not context.get("team_a_id"):
        return None
    return None


@app.route("/api/machine-chat-stream", methods=["POST"])
def api_machine_chat_stream():
    """SSE streaming variant of the draft agent — yields tool events then the final answer."""
    import time as _time
    from flask import Response, stream_with_context
    from draft_engine.llm import build_draft_system_prompt, stream_agent_loop, _AGENT_TOOLS
    from draft_engine.agent_tools import build_machine_tool_hint
    from draft_engine.agent_tool_runtime import (
        build_tool_executor,
        prefetch_site_payload,
        should_prefetch_site,
    )

    # Parse the request eagerly (request context not guaranteed inside generator)
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Message is required."}), 400
    if len(message) > 1200:
        return jsonify({"error": "Message is too long."}), 400

    pending_message = (payload.get("pending_message") or "").strip()
    intent_message = pending_message or message
    intent = _machine_agent_intent(intent_message)
    chat_context = _machine_agent_context_from_payload(payload, message)
    season_value = chat_context.get("season") or "all"
    selected_map_name = chat_context.get("map") or "all"
    selected_map_type = get_selected_map_type(chat_context.get("map_type") or "all")
    selected_mode_type = str(chat_context.get("mode_type") or "all").strip().lower() or "all"
    include_scrims = bool(chat_context.get("include_scrims", True))
    include_tournaments = bool(chat_context.get("include_tournaments", True))
    if not include_scrims and not include_tournaments:
        return jsonify({"error": "Select at least one data source."}), 400

    personal_team_obj = _machine_agent_get_personal_team() or {}
    personal_team_id = chat_context.get("team_a_id") or (personal_team_obj.get("id") if personal_team_obj else None)
    personal_team_name = chat_context.get("team_a_name") or (personal_team_obj.get("name") if personal_team_obj else "") or ""

    captured_meta: list[dict] = [{"has_matchup": False}]
    captured_visuals: list[dict] = [{}]
    captured_team_a_id: list = [chat_context.get("team_a_id")]
    captured_team_b_id: list = [chat_context.get("team_b_id")]
    tools_called: list[set] = [set()]
    prefetched_site_answer = ""
    prefetched_site_context = ""

    if should_prefetch_site(intent, chat_context, intent_message):
        prefetched = prefetch_site_payload(
            intent_message,
            season_value,
            site_answer_fn=_machine_agent_site_answer,
            site_context_text_fn=_machine_agent_site_context_text,
        )
        prefetched_site_answer = prefetched.get("answer") or ""
        prefetched_site_context = prefetched.get("context") or ""
        prefetched_meta = prefetched.get("meta") or {}
        if prefetched_meta.get("visuals"):
            captured_visuals[0].update(prefetched_meta["visuals"])

    tool_executor = build_tool_executor(
        season_value=season_value,
        selected_map_name=selected_map_name,
        chat_context=chat_context,
        personal_team_id=personal_team_id,
        include_scrims=include_scrims,
        include_tournaments=include_tournaments,
        tools_called=tools_called[0],
        captured_meta=captured_meta,
        captured_visuals=captured_visuals,
        captured_team_a_id=captured_team_a_id,
        captured_team_b_id=captured_team_b_id,
        resolve_team_by_name=_resolve_team_by_name,
        build_context_fn=lambda ta_id, tb_id, season, map_name: _machine_chat_build_context(
            ta_id,
            tb_id,
            season,
            map_name,
            include_scrims,
            include_tournaments,
            selected_map_type,
            selected_mode_type,
        ),
        site_answer_fn=_machine_agent_site_answer,
        site_context_text_fn=_machine_agent_site_context_text,
    )

    has_opponent = bool(chat_context.get("team_b_id") or chat_context.get("team_b_name"))
    opponent_display = chat_context.get("team_b_name") or ("id=" + str(chat_context.get("team_b_id")) if chat_context.get("team_b_id") else "not set")
    context_hint = build_machine_tool_hint(
        personal_team_name or "not set",
        opponent_display,
        season_value,
        selected_map_name,
    )
    draft_live_hint = _machine_agent_draft_live_context_hint(chat_context)
    if draft_live_hint:
        context_hint += "\n\n" + draft_live_hint
    if prefetched_site_context:
        context_hint += "\n\nPreloaded site data (use this directly if relevant):\n" + prefetched_site_context
    live_draft_active = bool((chat_context.get("draft_live") or {}).get("active"))
    live_tools = _AGENT_TOOLS
    live_decision_data = {}
    if live_draft_active and chat_context.get("team_a_id") and chat_context.get("team_b_id"):
        live_context_text, live_meta = _machine_chat_build_context(
            chat_context.get("team_a_id"),
            chat_context.get("team_b_id"),
            season_value,
            selected_map_name,
            include_scrims,
            include_tournaments,
            selected_map_type,
            selected_mode_type,
        )
        captured_meta[0] = live_meta
        captured_visuals[0] = dict((live_meta.get("visuals") or {}))
        captured_team_a_id[0] = chat_context.get("team_a_id")
        captured_team_b_id[0] = chat_context.get("team_b_id")
        live_packet_text, live_decision_data = _machine_agent_live_decision_packet(
            chat_context,
            captured_visuals[0],
        )
        context_hint += "\n\nPreloaded matchup data:\n" + live_context_text
        if live_packet_text:
            context_hint += "\n\n" + live_packet_text
        live_tools = []
    system_prompt = build_draft_system_prompt(
        context_text="",
        site_context_text=context_hint,
        personal_team=personal_team_name,
        meta={
            "has_matchup": has_opponent,
            "live_draft_active": live_draft_active,
        },
        intent=intent,
    )

    def _generate_events():
        import json as _json
        agent_answer = ""
        for event in stream_agent_loop(
            user_message=intent_message,
            system_prompt=system_prompt,
            tools=live_tools,
            tool_executor=tool_executor,
            temperature=0.25 if live_draft_active else 0.7,
            max_tokens=650 if live_draft_active else None,
            max_steps=1 if live_draft_active and not live_tools else (2 if live_draft_active else 5),
            timeout=10 if live_draft_active else 30,
            deadline_seconds=9.5 if live_draft_active else None,
        ):
            etype = event.get("type")
            if etype in ("tool_start", "tool_end"):
                yield f"data: {_json.dumps(event)}\n\n"
            elif etype == "text":
                agent_answer = event.get("text") or ""
            elif etype in ("done", "error"):
                if etype == "error":
                    yield f"data: {_json.dumps(event)}\n\n"
                    return
                # Run post-processing (same as _api_machine_chat_inner)
                meta = captured_meta[0]
                all_visuals = dict(captured_visuals[0])
                team_a_id = captured_team_a_id[0]
                team_b_id = captured_team_b_id[0]

                compare_request = _machine_agent_parse_slot_compare(intent_message) if meta.get("has_matchup") else None
                player_pivot_request = _machine_agent_parse_player_pivot(intent_message, chat_context) if meta.get("has_matchup") else None
                hero_focus = _machine_agent_parse_hero(intent_message) if meta.get("has_matchup") else ""

                live_draft_fallback = (
                    _machine_agent_live_decision_fallback(live_decision_data)
                    or _machine_agent_live_draft_fallback(chat_context)
                )
                fallback_text = live_draft_fallback or _machine_agent_answer_for_intent(intent_message, "", meta, intent)
                final_answer = _machine_agent_humanize_answer(agent_answer or prefetched_site_answer or fallback_text)
                if live_decision_data:
                    all_visuals["live_decision"] = live_decision_data

                if intent == "player_pivot" and not player_pivot_request:
                    final_answer = _machine_agent_humanize_answer(
                        agent_answer or _machine_agent_player_pivot_missing_response(intent_message, chat_context)
                    )
                    meta["intent"] = "player_pivot"
                    meta["visuals"] = {}
                elif player_pivot_request and team_a_id and team_b_id:
                    target_team_id = player_pivot_request.get("target_team_id") or team_b_id
                    target_row = get_db().execute("SELECT * FROM teams WHERE id = ?", (target_team_id,)).fetchone()
                    if target_row:
                        target_history = _machine_chat_filter_team_history(
                            target_row,
                            season_value,
                            selected_map_name,
                            include_scrims,
                            include_tournaments,
                            selected_map_type,
                            selected_mode_type,
                        )
                        pivot_stats = _machine_agent_player_pivot_stats(
                            target_history, player_pivot_request["player"], player_pivot_request["banned_hero"]
                        )
                        if not agent_answer:
                            final_answer = _machine_agent_humanize_answer(
                                _machine_agent_player_pivot_answer(pivot_stats, target_row["name"])
                            )
                        meta["intent"] = "player_pivot"
                        meta["visuals"] = {"player_pivot": pivot_stats}
                elif compare_request and team_a_id and team_b_id:
                    hero_name = compare_request["hero"]
                    slot_numbers = compare_request["slots"]
                    db = get_db()
                    team_a_row = db.execute("SELECT * FROM teams WHERE id = ?", (team_a_id,)).fetchone()
                    team_b_row = db.execute("SELECT * FROM teams WHERE id = ?", (team_b_id,)).fetchone()
                    if team_a_row and team_b_row:
                        team_a_stats = _machine_agent_slot_timing_stats(
                            _machine_chat_filter_team_history(
                                team_a_row,
                                season_value,
                                selected_map_name,
                                include_scrims,
                                include_tournaments,
                                selected_map_type,
                                selected_mode_type,
                            ),
                            meta.get("team_a", ""),
                            hero_name,
                            slot_numbers,
                        )
                        team_b_stats = _machine_agent_slot_timing_stats(
                            _machine_chat_filter_team_history(
                                team_b_row,
                                season_value,
                                selected_map_name,
                                include_scrims,
                                include_tournaments,
                                selected_map_type,
                                selected_mode_type,
                            ),
                            meta.get("team_b", ""),
                            hero_name,
                            slot_numbers,
                        )
                        if not agent_answer:
                            def _stats_line(ts: dict) -> str:
                                bits = [
                                    f"{s['label']}: {s['maps']} maps, {s['rate']}% of drafts, {s['win_rate']}% WR"
                                    for s in ts.get("slots", [])
                                ]
                                return f"{ts['team']} — " + "; ".join(bits) + f". Edge: {ts.get('leader') or 'no clear slot edge'}."
                            final_answer = _machine_agent_humanize_answer(
                                f"Slot timing: {hero_name}\n\n" + _stats_line(team_a_stats) + "\n" + _stats_line(team_b_stats)
                            )
                        meta["intent"] = "slot_compare"
                        meta["visuals"] = {"slot_compare": {"hero": hero_name, "teams": [team_a_stats, team_b_stats]}}
                else:
                    if hero_focus:
                        volatile_match = next(
                            (r for r in all_visuals.get("volatile_rows", [])
                             if (r.get("hero", "") or "").lower() == hero_focus.lower()),
                            None,
                        )
                        all_visuals["hero_focus"] = {
                            "hero": hero_focus,
                            "is_recommended_ban": hero_focus in all_visuals.get("recommended_bans", []),
                            "in_target_comp": hero_focus in all_visuals.get("target_comp", []),
                            "enemy_comfort": hero_focus in all_visuals.get("enemy_comfort", []),
                            "volatile": volatile_match,
                        }
                    meta["intent"] = intent
                    meta["visuals"] = _machine_agent_filter_visuals(intent, all_visuals)

                meta["context"] = {
                    **chat_context,
                    "team_a_name": meta.get("team_a") or chat_context.get("team_a_name", ""),
                    "team_b_name": meta.get("team_b") or chat_context.get("team_b_name", ""),
                }
                _machine_agent_hide_live_comps_until_complete(meta, chat_context)
                meta["reasoning_mode"] = "agentic"
                meta["response_engine"] = "openai" if agent_answer else "local_draft"

                done_event = {
                    "type": "done",
                    "answer": final_answer,
                    "meta": meta,
                }
                yield f"data: {_json.dumps(done_event)}\n\n"
                return

    def generate():
        import json as _json
        try:
            yield from _generate_events()
        except Exception as _exc:
            import traceback as _tb
            print(f"[machine] streaming chat failed: {_exc}\n{_tb.format_exc()}")
            yield f"data: {_json.dumps({'type': 'error', 'text': 'Chat stream failed while building the answer. Check Render logs for details.'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.route("/api/draft-agent", methods=["POST"])
@app.route("/api/machine-chat", methods=["POST"])
def api_machine_chat():
    try:
        return _api_machine_chat_inner()
    except Exception as _exc:
        import traceback
        traceback.print_exc()
        return jsonify({
            "answer": "Something went wrong processing that request. Try rephrasing or loading a different matchup.",
            "source": "error",
            "reason": "internal_error",
            "meta": {"has_matchup": False, "intent": "error", "needs_context": False, "visuals": {}},
        }), 200


def _api_machine_chat_inner():
    from draft_engine.llm import build_draft_system_prompt, run_agent_loop, _AGENT_TOOLS
    from draft_engine.agent_tools import build_machine_tool_hint
    from draft_engine.agent_tool_runtime import (
        build_tool_executor,
        prefetch_site_payload,
        should_prefetch_site,
    )

    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Message is required."}), 400
    if len(message) > 1200:
        return jsonify({"error": "Message is too long."}), 400

    pending_message = (payload.get("pending_message") or "").strip()
    intent_message = pending_message or message
    intent = _machine_agent_intent(intent_message)
    chat_context = _machine_agent_context_from_payload(payload, message)
    season_value = chat_context.get("season") or "all"
    selected_map_name = chat_context.get("map") or "all"
    selected_map_type = get_selected_map_type(chat_context.get("map_type") or "all")
    selected_mode_type = str(chat_context.get("mode_type") or "all").strip().lower() or "all"
    include_scrims = bool(chat_context.get("include_scrims", True))
    include_tournaments = bool(chat_context.get("include_tournaments", True))
    if not include_scrims and not include_tournaments:
        return jsonify({"error": "Select at least one data source."}), 400
    live_draft_active = bool((chat_context.get("draft_live") or {}).get("active"))

    cache_key = None
    if not live_draft_active and not pending_message:
        cache_key = (
            (intent_message or "").strip().lower(),
            str(intent),
            int(chat_context.get("team_a_id") or 0),
            int(chat_context.get("team_b_id") or 0),
            str(chat_context.get("season") or "all"),
            str(chat_context.get("map") or "all"),
            str(chat_context.get("map_type") or "all"),
            str(chat_context.get("mode_type") or "all"),
            bool(include_scrims),
            bool(include_tournaments),
        )
        now_ts = time.time()
        _machine_chat_response_cache_prune(now_ts)
        cached = _MACHINE_CHAT_RESPONSE_CACHE.get(cache_key)
        if cached and (now_ts - float(cached.get("ts") or 0)) <= _MACHINE_CHAT_RESPONSE_CACHE_TTL_SECONDS:
            return jsonify(cached.get("payload") or {})

    personal_team_obj = _machine_agent_get_personal_team() or {}
    personal_team_id = chat_context.get("team_a_id") or (personal_team_obj.get("id") if personal_team_obj else None)
    personal_team_name = chat_context.get("team_a_name") or (personal_team_obj.get("name") if personal_team_obj else "") or ""

    # Mutable state captured by tool executor (use single-element lists for nonlocal mutation)
    captured_meta: list[dict] = [{"has_matchup": False}]
    captured_visuals: list[dict] = [{}]
    captured_team_a_id: list = [chat_context.get("team_a_id")]
    captured_team_b_id: list = [chat_context.get("team_b_id")]
    tools_called: list[set] = [set()]
    prefetched_site_answer = ""
    prefetched_site_context = ""

    if should_prefetch_site(intent, chat_context, intent_message):
        prefetched = prefetch_site_payload(
            intent_message,
            season_value,
            site_answer_fn=_machine_agent_site_answer,
            site_context_text_fn=_machine_agent_site_context_text,
        )
        prefetched_site_answer = prefetched.get("answer") or ""
        prefetched_site_context = prefetched.get("context") or ""
        prefetched_meta = prefetched.get("meta") or {}
        if prefetched_meta.get("visuals"):
            captured_visuals[0].update(prefetched_meta["visuals"])

    tool_executor = build_tool_executor(
        season_value=season_value,
        selected_map_name=selected_map_name,
        chat_context=chat_context,
        personal_team_id=personal_team_id,
        include_scrims=include_scrims,
        include_tournaments=include_tournaments,
        tools_called=tools_called[0],
        captured_meta=captured_meta,
        captured_visuals=captured_visuals,
        captured_team_a_id=captured_team_a_id,
        captured_team_b_id=captured_team_b_id,
        resolve_team_by_name=_resolve_team_by_name,
        build_context_fn=lambda ta_id, tb_id, season, map_name: _machine_chat_build_context(
            ta_id,
            tb_id,
            season,
            map_name,
            include_scrims,
            include_tournaments,
            selected_map_type,
            selected_mode_type,
        ),
        site_answer_fn=_machine_agent_site_answer,
        site_context_text_fn=_machine_agent_site_context_text,
    )

    # Build a thin system prompt — the LLM will call tools to fetch what it needs
    has_opponent = bool(chat_context.get("team_b_id") or chat_context.get("team_b_name"))
    opponent_display = chat_context.get("team_b_name") or ("id=" + str(chat_context.get("team_b_id")) if chat_context.get("team_b_id") else "not set")
    context_hint = build_machine_tool_hint(
        personal_team_name or "not set",
        opponent_display,
        season_value,
        selected_map_name,
    )
    draft_live_hint = _machine_agent_draft_live_context_hint(chat_context)
    if draft_live_hint:
        context_hint += "\n\n" + draft_live_hint
    if prefetched_site_context:
        context_hint += "\n\nPreloaded site data (use this directly if relevant):\n" + prefetched_site_context
    live_tools = _AGENT_TOOLS
    live_decision_data = {}
    if live_draft_active and chat_context.get("team_a_id") and chat_context.get("team_b_id"):
        live_context_text, live_meta = _machine_chat_build_context(
            chat_context.get("team_a_id"),
            chat_context.get("team_b_id"),
            season_value,
            selected_map_name,
            include_scrims,
            include_tournaments,
            selected_map_type,
            selected_mode_type,
        )
        captured_meta[0] = live_meta
        captured_visuals[0] = dict((live_meta.get("visuals") or {}))
        captured_team_a_id[0] = chat_context.get("team_a_id")
        captured_team_b_id[0] = chat_context.get("team_b_id")
        live_packet_text, live_decision_data = _machine_agent_live_decision_packet(
            chat_context,
            captured_visuals[0],
        )
        context_hint += "\n\nPreloaded matchup data:\n" + live_context_text
        if live_packet_text:
            context_hint += "\n\n" + live_packet_text
        live_tools = []
    system_prompt = build_draft_system_prompt(
        context_text="",
        site_context_text=context_hint,
        personal_team=personal_team_name,
        meta={
            "has_matchup": has_opponent,
            "live_draft_active": live_draft_active,
        },
        intent=intent,
    )

    agent_answer = run_agent_loop(
        user_message=intent_message,
        system_prompt=system_prompt,
        tools=live_tools,
        tool_executor=tool_executor,
        temperature=0.25 if live_draft_active else 0.7,
        max_tokens=650 if live_draft_active else None,
        max_steps=1 if live_draft_active and not live_tools else (2 if live_draft_active else 5),
        timeout=10 if live_draft_active else 30,
        deadline_seconds=9.5 if live_draft_active else None,
    )

    # Retrieve state captured during tool calls
    meta = captured_meta[0]
    all_visuals = dict(captured_visuals[0])
    team_a_id = captured_team_a_id[0]
    team_b_id = captured_team_b_id[0]

    # Structured post-processing for player_pivot and slot_compare
    compare_request = _machine_agent_parse_slot_compare(intent_message) if meta.get("has_matchup") else None
    player_pivot_request = _machine_agent_parse_player_pivot(intent_message, chat_context) if meta.get("has_matchup") else None
    hero_focus = _machine_agent_parse_hero(intent_message) if meta.get("has_matchup") else ""

    live_draft_fallback = (
        _machine_agent_live_decision_fallback(live_decision_data)
        or _machine_agent_live_draft_fallback(chat_context)
    )
    fallback_text = live_draft_fallback or _machine_agent_answer_for_intent(intent_message, "", meta, intent)
    final_answer = _machine_agent_humanize_answer(agent_answer or prefetched_site_answer or fallback_text)
    if live_decision_data:
        all_visuals["live_decision"] = live_decision_data

    if intent == "player_pivot" and not player_pivot_request:
        final_answer = _machine_agent_humanize_answer(
            agent_answer or _machine_agent_player_pivot_missing_response(intent_message, chat_context)
        )
        meta["intent"] = "player_pivot"
        meta["visuals"] = {}
    elif player_pivot_request and team_a_id and team_b_id:
        target_team_id = player_pivot_request.get("target_team_id") or team_b_id
        target_row = get_db().execute("SELECT * FROM teams WHERE id = ?", (target_team_id,)).fetchone()
        if target_row:
            target_history = _machine_chat_filter_team_history(
                target_row,
                season_value,
                selected_map_name,
                include_scrims,
                include_tournaments,
                selected_map_type,
                selected_mode_type,
            )
            pivot_stats = _machine_agent_player_pivot_stats(
                target_history, player_pivot_request["player"], player_pivot_request["banned_hero"]
            )
            if not agent_answer:
                final_answer = _machine_agent_humanize_answer(
                    _machine_agent_player_pivot_answer(pivot_stats, target_row["name"])
                )
            meta["intent"] = "player_pivot"
            meta["visuals"] = {"player_pivot": pivot_stats}
    elif compare_request and team_a_id and team_b_id:
        hero_name = compare_request["hero"]
        slot_numbers = compare_request["slots"]
        db = get_db()
        team_a_row = db.execute("SELECT * FROM teams WHERE id = ?", (team_a_id,)).fetchone()
        team_b_row = db.execute("SELECT * FROM teams WHERE id = ?", (team_b_id,)).fetchone()
        if team_a_row and team_b_row:
            team_a_stats = _machine_agent_slot_timing_stats(
                _machine_chat_filter_team_history(
                    team_a_row,
                    season_value,
                    selected_map_name,
                    include_scrims,
                    include_tournaments,
                    selected_map_type,
                    selected_mode_type,
                ),
                meta.get("team_a", ""),
                hero_name,
                slot_numbers,
            )
            team_b_stats = _machine_agent_slot_timing_stats(
                _machine_chat_filter_team_history(
                    team_b_row,
                    season_value,
                    selected_map_name,
                    include_scrims,
                    include_tournaments,
                    selected_map_type,
                    selected_mode_type,
                ),
                meta.get("team_b", ""),
                hero_name,
                slot_numbers,
            )
            if not agent_answer:
                def _stats_line(ts: dict) -> str:
                    bits = [
                        f"{s['label']}: {s['maps']} maps, {s['rate']}% of drafts, {s['win_rate']}% WR"
                        for s in ts.get("slots", [])
                    ]
                    return f"{ts['team']} — " + "; ".join(bits) + f". Edge: {ts.get('leader') or 'no clear slot edge'}."
                final_answer = _machine_agent_humanize_answer(
                    f"Slot timing: {hero_name}\n\n" + _stats_line(team_a_stats) + "\n" + _stats_line(team_b_stats)
                )
            meta["intent"] = "slot_compare"
            meta["visuals"] = {"slot_compare": {"hero": hero_name, "teams": [team_a_stats, team_b_stats]}}
    else:
        if hero_focus:
            volatile_match = next(
                (r for r in all_visuals.get("volatile_rows", [])
                 if (r.get("hero", "") or "").lower() == hero_focus.lower()),
                None,
            )
            all_visuals["hero_focus"] = {
                "hero": hero_focus,
                "is_recommended_ban": hero_focus in all_visuals.get("recommended_bans", []),
                "in_target_comp": hero_focus in all_visuals.get("target_comp", []),
                "enemy_comfort": hero_focus in all_visuals.get("enemy_comfort", []),
                "volatile": volatile_match,
            }
        meta["intent"] = intent
        meta["visuals"] = _machine_agent_filter_visuals(intent, all_visuals)

    meta["context"] = {
        **chat_context,
        "team_a_name": meta.get("team_a") or chat_context.get("team_a_name", ""),
        "team_b_name": meta.get("team_b") or chat_context.get("team_b_name", ""),
    }
    _machine_agent_hide_live_comps_until_complete(meta, chat_context)
    meta["reasoning_mode"] = "agentic"
    meta["response_engine"] = "openai" if agent_answer else "local_draft"

    response_payload = {
        "answer": final_answer,
        "source": "draft_agent",
        "reason": None,
        "meta": meta,
    }
    if cache_key is not None:
        _MACHINE_CHAT_RESPONSE_CACHE[cache_key] = {"ts": time.time(), "payload": response_payload}

    return jsonify(response_payload)


@app.route("/api/draft-reasoner/enemy-scouting")
def api_draft_reasoner_enemy_scouting():
    """Return tournament loss analysis for the enemy team.

    For each map where team_b lost to another team, aggregate:
    - bans the winning team used against team_b
    - heroes the winning team played on those maps
    - heroes team_b themselves played on those losing maps (shows what didn't work)
    Only includes season-filtered data when season is specified.
    """
    db = get_db()
    team_b_id = request.args.get("team_b", type=int)
    season_value = (request.args.get("season", "") or "").strip()
    if not team_b_id:
        return jsonify({"error": "team_b is required"}), 400

    team_b_row = db.execute("SELECT * FROM teams WHERE id = ?", (team_b_id,)).fetchone()
    if team_b_row is None:
        abort(404)

    team_id = int(team_b_row["id"])
    team_name = (team_b_row["name"] or "").strip().lower()

    # Counts indexed by hero name
    ban_counts: defaultdict[str, int] = defaultdict(int)   # bans by winning teams vs team_b
    winner_hero_counts: defaultdict[str, int] = defaultdict(int)  # heroes winning teams played
    loser_hero_counts: defaultdict[str, int] = defaultdict(int)   # heroes team_b played when losing
    loss_map_total = 0
    match_summaries: list[dict] = []

    for tournament_record in TOURNAMENT_MATCHES:
        # Season filter
        rec_season = normalize_season_value(tournament_record.get("season", ""))
        if season_value and season_value.lower() not in ("all", "") and rec_season:
            if season_value != rec_season:
                continue

        # Find team_b's entry in this tournament
        team_b_entry: dict | None = None
        for t_team in tournament_record.get("tournament_teams", []):
            if not isinstance(t_team, dict):
                continue
            src_id = t_team.get("source_team_id")
            t_name = (t_team.get("name") or "").strip().lower()
            if (isinstance(src_id, int) and src_id == team_id) or (not src_id and t_name and t_name == team_name):
                team_b_entry = t_team
                break

        if team_b_entry is None:
            continue

        b_t_id = team_b_entry.get("id")

        for match in tournament_record.get("matches", []):
            if not isinstance(match, dict):
                continue

            # Determine which slot team_b occupies
            if match.get("team1_tournament_team_id") == b_t_id:
                b_slot, w_slot = "team1", "team2"
                opponent_name = (match.get("team2_name") or "").strip() or "Opponent"
            elif match.get("team2_tournament_team_id") == b_t_id:
                b_slot, w_slot = "team2", "team1"
                opponent_name = (match.get("team1_name") or "").strip() or "Opponent"
            else:
                continue

            map_losses: list[str] = []
            for map_entry in match.get("maps", []):
                if not isinstance(map_entry, dict):
                    continue

                outcome = get_map_outcome_for_slot(map_entry, b_slot)
                if outcome != "Loss":
                    continue

                loss_map_total += 1
                map_name = (map_entry.get("map_name") or map_entry.get("map") or "").strip()
                if map_name:
                    map_losses.append(map_name)

                # Winning team's bans
                draft_data = map_entry.get("draft", {})
                if isinstance(draft_data, dict):
                    w_draft = draft_data.get(w_slot, {})
                    if isinstance(w_draft, dict):
                        for slot_key, hero_val in w_draft.items():
                            if slot_key.startswith("ban") and hero_val:
                                h = _canonical_draft_hero(hero_val)
                                if h:
                                    ban_counts[h] += 1

                # Winning team's heroes played
                for section in map_entry.get("comp", []):
                    if not isinstance(section, dict):
                        continue
                    for slot in section.get(w_slot, []):
                        if isinstance(slot, dict):
                            h = _canonical_draft_hero(slot.get("hero", ""))
                            if h:
                                winner_hero_counts[h] += 1
                    # Team_b heroes on losing maps
                    for slot in section.get(b_slot, []):
                        if isinstance(slot, dict):
                            h = _canonical_draft_hero(slot.get("hero", ""))
                            if h:
                                loser_hero_counts[h] += 1

            if map_losses:
                match_summaries.append({
                    "opponent": opponent_name,
                    "maps_lost": map_losses,
                    "count": len(map_losses),
                })

    match_summaries.sort(key=lambda r: r["count"], reverse=True)

    def _to_rows(counts: dict[str, int], total_maps: int) -> list[dict]:
        rows = [
            {
                "hero": h,
                "count": c,
                "rate": round((c / total_maps) * 100, 1) if total_maps else 0.0,
            }
            for h, c in counts.items()
        ]
        rows.sort(key=lambda r: r["count"], reverse=True)
        return rows[:15]

    return jsonify({
        "team_b_name": (team_b_row["name"] or "").strip(),
        "loss_maps_total": loss_map_total,
        "winning_team_bans": _to_rows(ban_counts, loss_map_total),
        "winning_team_heroes": _to_rows(winner_hero_counts, loss_map_total),
        "enemy_heroes_when_losing": _to_rows(loser_hero_counts, loss_map_total),
        "match_summaries": match_summaries[:10],
    })


@app.route("/api/draft-reasoner/model")
def api_draft_reasoner_model():
    """Return the full matchup model plus per-team comfort/ban/comp data for the reasoner."""
    db = get_db()
    team_a_id = request.args.get("team_a", type=int)
    team_b_id = request.args.get("team_b", type=int)
    if not team_a_id or not team_b_id:
        return jsonify({"error": "Both team_a and team_b are required."}), 400

    team_rows = db.execute(
        "SELECT * FROM teams WHERE id IN (?, ?)", (team_a_id, team_b_id)
    ).fetchall()
    team_lookup = {row["id"]: row for row in team_rows}
    team_a = team_lookup.get(team_a_id)
    team_b = team_lookup.get(team_b_id)
    if team_a is None or team_b is None:
        abort(404)

    season_value = request.args.get("season", "")
    selected_map_type = get_selected_map_type(request.args.get("map_type", "all"))
    selected_mode_type = str(request.args.get("mode_type", "all") or "all").strip().lower() or "all"
    if selected_mode_type not in {"all", "control", "escort", "hybrid"}:
        selected_mode_type = "all"
    selected_map_name = (request.args.get("map", "") or "").strip()
    include_scrims = _bool_arg("include_scrims", True)
    include_tournaments = _bool_arg("include_tournaments", True)
    include_player_rows = _bool_arg("include_player_rows", True)

    now_ts = time.time()
    cache_key = (
        int(team_a_id),
        int(team_b_id),
        str((season_value or "all").strip().lower() or "all"),
        str((selected_map_type or "all").strip().lower() or "all"),
        str((selected_mode_type or "all").strip().lower() or "all"),
        str((selected_map_name or "all").strip().lower() or "all"),
        bool(include_scrims),
        bool(include_tournaments),
        bool(include_player_rows),
    )
    _draft_reasoner_cache_prune(now_ts)
    cached = _DRAFT_REASONER_MODEL_CACHE.get(cache_key)
    if cached and (now_ts - float(cached.get("ts") or 0)) <= _DRAFT_REASONER_MODEL_CACHE_TTL_SECONDS:
        return jsonify(cached.get("payload") or {})

    def _get_filtered_scrims(team_row) -> tuple[list[dict], list[dict]]:
        def _filter_scrims_by_mode_type(scrims: list[dict], mode_type_value: str) -> list[dict]:
            if mode_type_value == "all":
                return scrims
            mode_lookup = {
                str(map_name or "").strip().lower(): str(mode_name or "").strip().lower()
                for map_name, mode_name in (MAP_MODES or {}).items()
            }
            filtered_scrims: list[dict] = []
            for scrim in scrims or []:
                if not isinstance(scrim, dict):
                    continue
                filtered_maps = []
                for map_entry in scrim.get("maps", []) or []:
                    if not isinstance(map_entry, dict):
                        continue
                    map_mode = str(map_entry.get("mode") or map_entry.get("map_mode") or "").strip().lower()
                    if not map_mode:
                        map_name_key = str(map_entry.get("map") or map_entry.get("map_name") or "").strip().lower()
                        map_mode = mode_lookup.get(map_name_key, "")
                    if map_mode == mode_type_value:
                        filtered_maps.append(map_entry)
                if filtered_maps:
                    scrim_copy = dict(scrim)
                    scrim_copy["maps"] = filtered_maps
                    filtered_scrims.append(scrim_copy)
            return filtered_scrims

        all_scrims = get_team_history_for_sources(
            team_row,
            include_scrims=include_scrims,
            include_tournaments=include_tournaments,
        )
        season_options = get_scrim_season_options(all_scrims)
        default_season = get_current_season_from_recent_scrim(all_scrims)
        has_unseasoned = any(not normalize_season_value(s.get("season", "")) for s in all_scrims)
        effective_default = "all" if not season_value or season_value.lower() == "all" else default_season
        selected_season = get_selected_season(
            season_value,
            season_options,
            allow_unspecified=has_unseasoned,
            default_season=effective_default,
            strict=True,
        )
        scrims = filter_scrims_by_season(all_scrims, selected_season)
        scrims = filter_scrims_by_map_type(scrims, selected_map_type)
        scrims = _filter_scrims_by_mode_type(scrims, selected_mode_type)
        hero_pool_scrims = filter_scrims_by_season(all_scrims, selected_season)
        hero_pool_scrims = filter_scrims_by_map_type(hero_pool_scrims, selected_map_type)
        hero_pool_scrims = _filter_scrims_by_mode_type(hero_pool_scrims, selected_mode_type)
        if selected_map_name and selected_map_name.lower() != "all":
            filtered = []
            for scrim in scrims:
                scrim_copy = dict(scrim)
                scrim_copy["maps"] = [
                    m for m in scrim.get("maps", [])
                    if ((m.get("map") or m.get("map_name") or "").strip().lower() == selected_map_name.lower())
                ]
                if scrim_copy["maps"]:
                    filtered.append(scrim_copy)
            return filtered, hero_pool_scrims
        return scrims, hero_pool_scrims

    def _get_team_roster(team_id: int) -> list[dict]:
        rows = db.execute(
            """
            SELECT name, role, main_hero, COALESCE(is_sub, 0) AS is_sub
            FROM players
            WHERE team_id = ?
              AND role NOT IN ('Coach', 'AC', 'Analyst')
            ORDER BY COALESCE(is_sub, 0), name COLLATE NOCASE
            """,
            (team_id,),
        ).fetchall()
        return [
            {
                "name": (row["name"] or "").strip(),
                "role": (row["role"] or "").strip(),
                "main_hero": normalize_hero_slot_value(row["main_hero"] or ""),
                "is_sub": bool(row["is_sub"]),
            }
            for row in rows
            if (row["name"] or "").strip()
        ]

    a_scrims, a_hero_pool_scrims = _get_filtered_scrims(team_a)
    b_scrims, b_hero_pool_scrims = _get_filtered_scrims(team_b)
    matchup = build_matchup_tree_model(
        team_a["name"],
        a_scrims,
        team_b["name"],
        b_scrims,
        team_a_hero_pool_scrims=a_hero_pool_scrims,
        team_b_hero_pool_scrims=b_hero_pool_scrims,
    )
    teams_payload = matchup.get("teams", [])

    def _build_player_hero_rows(roster: list[dict], hero_pool_scrims: list[dict]) -> list[dict]:
        rows = []
        for player in roster:
            player_name = (player.get("name") or "").strip()
            if not player_name:
                continue
            bd = build_player_hero_map_breakdown(
                player_name,
                hero_pool_scrims,
                team_slots=TEAM_SLOTS,
                canonical_draft_hero=_canonical_draft_hero,
                get_map_outcome_for_slot=get_map_outcome_for_slot,
                map_modes=MAP_MODES,
                get_map_image_url=get_map_image_url,
            )
            hero_rows = bd.get("hero_rows", [])
            total_maps = round(sum(h.get("maps", 0) for h in hero_rows), 2)
            top_heroes = [
                {
                    "hero": h["hero"],
                    "maps": h["maps"],
                    "appearances": h["maps"],
                    "usage_rate": round((h["maps"] / total_maps) * 100, 1) if total_maps else 0.0,
                    "win_rate": h.get("win_rate", 0),
                }
                for h in hero_rows
            ]
            rows.append({
                "player": player_name,
                "maps": total_maps,
                "appearances": total_maps,
                "overall_win_rate": 0.0,
                "top_heroes": top_heroes,
            })
        rows.sort(key=lambda r: (r["maps"], r["player"].lower()), reverse=True)
        return rows

    if len(teams_payload) >= 1:
        roster_a = _get_team_roster(team_a_id)
        teams_payload[0]["roster_players"] = roster_a
        teams_payload[0].setdefault("model", {})["player_hero_rows"] = (
            _build_player_hero_rows(roster_a, a_hero_pool_scrims) if include_player_rows else []
        )
    if len(teams_payload) >= 2:
        roster_b = _get_team_roster(team_b_id)
        teams_payload[1]["roster_players"] = roster_b
        teams_payload[1].setdefault("model", {})["player_hero_rows"] = (
            _build_player_hero_rows(roster_b, b_hero_pool_scrims) if include_player_rows else []
        )

    _DRAFT_REASONER_MODEL_CACHE[cache_key] = {"ts": now_ts, "payload": matchup}
    return jsonify(matchup)
