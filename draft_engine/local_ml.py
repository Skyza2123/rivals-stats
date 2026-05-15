"""
local_ml.py — Pure local machine-learning engine for the draft agent.

ML layers
---------
1. Association Rules  (pair co-occurrence, lift-sorted) — de_ally/enemy_pair_stats
2. K-Means Clustering — groups hero comps into archetypes per team
3. Naive Bayes        — predicts next hero pick given current draft state
4. XGBoost            — three sub-models:
     • win_model    : predicts match win probability from ally+enemy hero vectors
     • map_model    : predicts best map given comp context
     • ban_model    : scores ban priority for each enemy hero

All models are trained lazily from SQLite on first use and cached in-process.
No external APIs. No pre-trained weights shipped.

Public API
----------
generate_local_answer(message, context_text, site_context_text, meta, intent,
                      personal_team, db, season) -> str

predict_next_pick(team_name, picked_so_far, db, season, top_n) -> list[dict]
cluster_comps(team_name, db, season, n_clusters) -> list[dict]
predict_win(our_heroes, enemy_heroes, db) -> float
predict_map_win_probs(our_heroes, db) -> list[dict]
predict_ban_priority(enemy_team_name, db, season) -> list[dict]
"""

from __future__ import annotations

import re
import hashlib
import threading
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# In-process model cache  (keyed by cache_key string → fitted object)
# ---------------------------------------------------------------------------

_MODEL_CACHE: dict[str, Any] = {}
_MODEL_LOCK = threading.Lock()


def _cache_get(key: str) -> Any | None:
    return _MODEL_CACHE.get(key)


def _cache_set(key: str, value: Any) -> None:
    with _MODEL_LOCK:
        _MODEL_CACHE[key] = value


def _cache_key(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Hero vocabulary helpers
# ---------------------------------------------------------------------------

def _all_heroes(db) -> list[str]:
    """Return all known hero names sorted — used as feature columns."""
    try:
        rows = db.execute(
            "SELECT DISTINCT hero FROM de_team_hero_bias ORDER BY hero"
        ).fetchall()
        return [r["hero"] for r in rows if r["hero"]]
    except Exception:
        return []


def _hero_vector(heroes: list[str], vocab: list[str]) -> np.ndarray:
    """Binary presence vector for `heroes` over `vocab`."""
    vec = np.zeros(len(vocab), dtype=np.float32)
    lower_map = {h.lower(): i for i, h in enumerate(vocab)}
    for h in heroes:
        idx = lower_map.get((h or "").lower())
        if idx is not None:
            vec[idx] = 1.0
    return vec


# ---------------------------------------------------------------------------
# 1. Association Rules  (unchanged from v1, kept for NLG enrichment)
# ---------------------------------------------------------------------------

def mine_association_rules(
    team_name: str,
    db,
    season: str = "all",
    is_enemy: bool = False,
    min_co: int = 2,
    top_n: int = 12,
) -> list[dict]:
    """
    Lift-sorted hero pair rules from de_ally_pair_stats / de_enemy_pair_stats.
    Returns list of {hero_a, hero_b, support, confidence, lift, win_rate, co_appearances}.
    """
    table = "de_enemy_pair_stats" if is_enemy else "de_ally_pair_stats"
    team_col = "enemy_team_name" if is_enemy else "team_name"
    try:
        if season and season.lower() != "all":
            rows = db.execute(
                f"SELECT hero_a, hero_b, co_appearances, wins, losses "
                f"FROM {table} WHERE {team_col}=? AND season=? AND co_appearances>=?",
                (team_name, season, min_co),
            ).fetchall()
        else:
            rows = db.execute(
                f"SELECT hero_a, hero_b, "
                f"SUM(co_appearances) AS co_appearances, SUM(wins) AS wins, SUM(losses) AS losses "
                f"FROM {table} WHERE {team_col}=? AND co_appearances>=? GROUP BY hero_a, hero_b",
                (team_name, min_co),
            ).fetchall()
    except Exception:
        return []
    if not rows:
        return []
    total_co = sum(max(r["co_appearances"], 1) for r in rows)
    avg_co = total_co / len(rows)
    rules = []
    for r in rows:
        co = r["co_appearances"] or 0
        wins = r["wins"] or 0
        losses = r["losses"] or 0
        total = wins + losses
        wr = round(wins / total * 100, 1) if total else 0.0
        support = co / avg_co
        confidence = wr / 100.0
        lift = round(confidence / 0.5, 3) if confidence else 0.0
        rules.append({
            "hero_a": r["hero_a"], "hero_b": r["hero_b"],
            "support": round(support, 3), "confidence": confidence,
            "lift": lift, "win_rate": wr, "co_appearances": co,
        })
    rules.sort(key=lambda x: (-x["lift"], -x["co_appearances"]))
    return rules[:top_n]


# ---------------------------------------------------------------------------
# 2. K-Means — comp archetype clustering
# ---------------------------------------------------------------------------

_ARCHETYPE_LABELS = {
    0: "Dive",
    1: "Poke / Peel",
    2: "Brawl",
    3: "Sustain / Bunker",
    4: "Flex / Control",
}

_DIVE_HEROES = {"Black Panther", "Spider-Man", "Psylocke", "Magik", "Iron Fist", "Winter Soldier", "Black Widow", "Cloak & Dagger"}
_POKE_HEROES  = {"Hawkeye", "Black Widow", "Hela", "Star-Lord", "Luna Snow", "Loki"}
_BRAWL_HEROES = {"Hulk", "Venom", "Thor", "Captain America", "Thing", "Groot"}
_SUSTAIN_HEROES = {"Mantis", "Adam Warlock", "Rocket Raccoon", "Jeff the Land Shark", "Invisible Woman"}


def _label_cluster(centroid: np.ndarray, vocab: list[str]) -> str:
    """Heuristically label a cluster centroid based on hero presence."""
    top_idx = np.argsort(centroid)[::-1][:8]
    top_heroes = {vocab[i] for i in top_idx if i < len(vocab)}
    scores = {
        "Dive": len(top_heroes & _DIVE_HEROES),
        "Poke / Peel": len(top_heroes & _POKE_HEROES),
        "Brawl": len(top_heroes & _BRAWL_HEROES),
        "Sustain / Bunker": len(top_heroes & _SUSTAIN_HEROES),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "Flex / Control"


def _load_comp_matrix(team_name: str, db, season: str) -> tuple[np.ndarray, list[str], list[str]]:
    """
    Build a (n_comps × n_heroes) matrix from de_trio_shell_stats rows.
    Each row represents one observed 3-hero shell (repeated by appearances).
    Returns (matrix, vocab, comp_ids).
    """
    vocab = _all_heroes(db)
    if not vocab:
        return np.empty((0, 0)), vocab, []
    try:
        if season and season.lower() != "all":
            rows = db.execute(
                "SELECT hero_a, hero_b, hero_c, co_appearances FROM de_trio_shell_stats "
                "WHERE team_name=? AND season=? AND co_appearances>=2",
                (team_name, season),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT hero_a, hero_b, hero_c, SUM(co_appearances) AS co_appearances "
                "FROM de_trio_shell_stats WHERE team_name=? GROUP BY hero_a, hero_b, hero_c "
                "HAVING co_appearances>=2",
                (team_name,),
            ).fetchall()
    except Exception:
        return np.empty((0, 0)), vocab, []

    expanded_rows = []
    comp_ids = []
    for r in rows:
        vec = _hero_vector([r["hero_a"], r["hero_b"], r["hero_c"]], vocab)
        for _ in range(min(r["co_appearances"], 10)):  # cap repetition
            expanded_rows.append(vec)
            comp_ids.append(f"{r['hero_a']}/{r['hero_b']}/{r['hero_c']}")
    if not expanded_rows:
        return np.empty((0, len(vocab))), vocab, []
    return np.vstack(expanded_rows), vocab, comp_ids


def cluster_comps(
    team_name: str,
    db,
    season: str = "all",
    n_clusters: int = 4,
) -> list[dict]:
    """
    K-Means cluster the team's observed hero shells into archetypes.
    Returns list of {archetype, top_heroes, count, win_rate}.
    """
    cache_key = _cache_key("kmeans", team_name, season, n_clusters)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    X, vocab, comp_ids = _load_comp_matrix(team_name, db, season)
    if X.shape[0] < max(n_clusters, 3):
        return []

    from sklearn.cluster import KMeans
    from sklearn.preprocessing import normalize

    Xn = normalize(X, norm="l2")
    k = min(n_clusters, X.shape[0])
    km = KMeans(n_clusters=k, n_init=10, random_state=42)
    labels = km.fit_predict(Xn)

    # Fetch win data from de_trio_shell_stats for each row
    try:
        if season and season.lower() != "all":
            wr_rows = db.execute(
                "SELECT hero_a, hero_b, hero_c, co_appearances, wins FROM de_trio_shell_stats "
                "WHERE team_name=? AND season=? AND co_appearances>=2",
                (team_name, season),
            ).fetchall()
        else:
            wr_rows = db.execute(
                "SELECT hero_a, hero_b, hero_c, SUM(co_appearances) AS co_appearances, SUM(wins) AS wins "
                "FROM de_trio_shell_stats WHERE team_name=? GROUP BY hero_a, hero_b, hero_c "
                "HAVING co_appearances>=2",
                (team_name,),
            ).fetchall()
        wr_map = {
            f"{r['hero_a']}/{r['hero_b']}/{r['hero_c']}": (r["co_appearances"], r["wins"])
            for r in wr_rows
        }
    except Exception:
        wr_map = {}

    cluster_data: dict[int, dict] = {}
    for i, lbl in enumerate(labels):
        cid = comp_ids[i]
        co, wins = wr_map.get(cid, (1, 0))
        d = cluster_data.setdefault(int(lbl), {"count": 0, "wins": 0, "centroid": km.cluster_centers_[lbl]})
        d["count"] += 1
        d["wins"] += wins / max(co, 1)

    results = []
    for lbl, d in sorted(cluster_data.items(), key=lambda x: -x[1]["count"]):
        centroid = d["centroid"]
        top_idx = np.argsort(centroid)[::-1][:6]
        top_heroes = [vocab[i] for i in top_idx if i < len(vocab) and centroid[i] > 0.01]
        archetype = _label_cluster(centroid, vocab)
        wr = round(d["wins"] / d["count"] * 100, 1) if d["count"] else 0.0
        results.append({
            "archetype": archetype,
            "top_heroes": top_heroes,
            "count": d["count"],
            "win_rate": wr,
        })

    _cache_set(cache_key, results)
    return results


# ---------------------------------------------------------------------------
# 3. Naive Bayes — next hero pick prediction
# ---------------------------------------------------------------------------

def _load_draft_sequences(team_name: str, db, season: str, is_enemy: bool = False) -> list[list[str]]:
    """
    Load ordered pick sequences from de_draft_actions.
    Returns list of [hero1, hero2, ...] in action_order for each map.
    """
    team_filter = "enemy_team_name" if is_enemy else "scrim_id"
    try:
        if is_enemy:
            if season and season.lower() != "all":
                rows = db.execute(
                    "SELECT de_map_id, hero, action_order FROM de_draft_actions "
                    "WHERE enemy_team_name=? AND action_type='pick' AND season=? "
                    "ORDER BY de_map_id, action_order",
                    (team_name, season),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT de_map_id, hero, action_order FROM de_draft_actions "
                    "WHERE enemy_team_name=? AND action_type='pick' "
                    "ORDER BY de_map_id, action_order",
                    (team_name,),
                ).fetchall()
        else:
            # "our" team — match by team_name stored in de_maps joined to de_draft_actions
            if season and season.lower() != "all":
                rows = db.execute(
                    "SELECT da.de_map_id, da.hero, da.action_order "
                    "FROM de_draft_actions da JOIN de_maps dm ON da.de_map_id=dm.id "
                    "WHERE dm.team_name=? AND da.action_type='pick' AND da.season=? "
                    "ORDER BY da.de_map_id, da.action_order",
                    (team_name, season),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT da.de_map_id, da.hero, da.action_order "
                    "FROM de_draft_actions da JOIN de_maps dm ON da.de_map_id=dm.id "
                    "WHERE dm.team_name=? AND da.action_type='pick' "
                    "ORDER BY da.de_map_id, da.action_order",
                    (team_name,),
                ).fetchall()
    except Exception:
        return []

    from collections import defaultdict
    seq_map: dict[int, list] = defaultdict(list)
    for r in rows:
        seq_map[r["de_map_id"]].append((r["action_order"], r["hero"]))
    sequences = []
    for mid, picks in seq_map.items():
        picks.sort(key=lambda x: x[0])
        sequences.append([p[1] for p in picks])
    return sequences


def _fit_naive_bayes(sequences: list[list[str]], vocab: list[str]):
    """
    Train a Multinomial Naive Bayes where:
      X[i] = binary hero vector of picks so far (all but last)
      y[i] = index of the next (last) pick
    Returns (clf, vocab) or (None, vocab) if insufficient data.
    """
    from sklearn.naive_bayes import MultinomialNB

    X_rows, y_rows = [], []
    for seq in sequences:
        for j in range(1, len(seq)):
            context = seq[:j]
            next_hero = seq[j]
            if next_hero not in vocab:
                continue
            vec = _hero_vector(context, vocab)
            X_rows.append(vec)
            y_rows.append(vocab.index(next_hero))

    if len(X_rows) < 5:
        return None, vocab

    X = np.vstack(X_rows).astype(np.float32)
    y = np.array(y_rows, dtype=np.int32)
    clf = MultinomialNB(alpha=1.0)
    clf.fit(X, y)
    return clf, vocab


def predict_next_pick(
    team_name: str,
    picked_so_far: list[str],
    db,
    season: str = "all",
    top_n: int = 5,
    is_enemy: bool = False,
) -> list[dict]:
    """
    Naive Bayes: given heroes already picked, return top_n likely next picks.
    Returns list of {hero, probability}.
    """
    vocab = _all_heroes(db)
    if not vocab:
        return []

    cache_key = _cache_key("nb", team_name, season, int(is_enemy))
    clf = _cache_get(cache_key)
    if clf is None:
        seqs = _load_draft_sequences(team_name, db, season, is_enemy=is_enemy)
        clf, _ = _fit_naive_bayes(seqs, vocab)
        _cache_set(cache_key, clf)

    if clf is None:
        return []

    try:
        vec = _hero_vector(picked_so_far, vocab).reshape(1, -1)
        probs = clf.predict_proba(vec)[0]
        # Zero out already-picked heroes
        picked_lower = {h.lower() for h in picked_so_far}
        top_idx = np.argsort(probs)[::-1]
        results = []
        for idx in top_idx:
            hero = vocab[idx]
            if hero.lower() in picked_lower:
                continue
            results.append({"hero": hero, "probability": round(float(probs[idx]), 4)})
            if len(results) >= top_n:
                break
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 4. XGBoost — win prediction, map selection, ban priority
# ---------------------------------------------------------------------------

def _load_map_records(db) -> tuple[list, list]:
    """
    Load (ally_hero_vec, enemy_hero_vec, result) from de_maps + de_draft_actions.
    Returns (X_list, y_list).
    """
    vocab = _all_heroes(db)
    if not vocab:
        return [], []
    try:
        map_rows = db.execute(
            "SELECT id, result, team_name, enemy_team_name FROM de_maps WHERE result IN ('Win','Loss')"
        ).fetchall()
    except Exception:
        return [], []

    try:
        action_rows = db.execute(
            "SELECT de_map_id, team_slot, hero, action_type FROM de_draft_actions WHERE action_type='pick'"
        ).fetchall()
    except Exception:
        return [], []

    from collections import defaultdict
    picks_by_map: dict[int, dict] = defaultdict(lambda: {"team1": [], "team2": []})
    for r in action_rows:
        picks_by_map[r["de_map_id"]][r["team_slot"]].append(r["hero"])

    our_team_rows = db.execute(
        """
        SELECT id, name
        FROM teams
        WHERE COALESCE(quality_tag, '') = 'Preferred'
        ORDER BY COALESCE(sort_order, 0), name COLLATE NOCASE
        LIMIT 1
        """
    ).fetchall() if db else []
    if not our_team_rows and db:
        our_team_rows = db.execute("SELECT id, name FROM teams WHERE is_personal=1 LIMIT 1").fetchall()
    our_team_name = our_team_rows[0]["name"].lower() if our_team_rows else ""

    X, y = [], []
    for m in map_rows:
        map_id = m["id"]
        result = 1 if m["result"] == "Win" else 0
        slots = picks_by_map.get(map_id, {"team1": [], "team2": []})
        # Determine which slot is ours
        t_name = (m["team_name"] or "").lower()
        if our_team_name and t_name == our_team_name:
            our_slot, enemy_slot = "team1", "team2"
        else:
            our_slot, enemy_slot = "team1", "team2"
        ally_vec = _hero_vector(slots[our_slot], vocab)
        enemy_vec = _hero_vector(slots[enemy_slot], vocab)
        X.append(np.concatenate([ally_vec, enemy_vec]))
        y.append(result)
    return X, y


def _fit_xgb_win(db):
    """Fit XGBoost win-prediction model on map records. Returns fitted booster or None."""
    try:
        import xgboost as xgb
    except ImportError:
        return None

    X_list, y_list = _load_map_records(db)
    if len(X_list) < 10:
        return None

    X = np.vstack(X_list).astype(np.float32)
    y = np.array(y_list, dtype=np.int32)
    dtrain = xgb.DMatrix(X, label=y)
    params = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "max_depth": 3,
        "eta": 0.2,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "seed": 42,
        "verbosity": 0,
    }
    model = xgb.train(params, dtrain, num_boost_round=40, verbose_eval=False)
    return model


def predict_win(
    our_heroes: list[str],
    enemy_heroes: list[str],
    db,
) -> float:
    """
    XGBoost: predict win probability for (our_heroes vs enemy_heroes).
    Returns probability in [0, 1], or 0.5 if model unavailable.
    """
    vocab = _all_heroes(db)
    if not vocab:
        return 0.5

    cache_key = _cache_key("xgb_win", "global")
    model = _cache_get(cache_key)
    if model is None:
        model = _fit_xgb_win(db)
        _cache_set(cache_key, model)

    if model is None:
        return 0.5

    try:
        import xgboost as xgb
        ally_vec = _hero_vector(our_heroes, vocab)
        enemy_vec = _hero_vector(enemy_heroes, vocab)
        X = np.concatenate([ally_vec, enemy_vec]).reshape(1, -1).astype(np.float32)
        dtest = xgb.DMatrix(X)
        prob = float(model.predict(dtest)[0])
        return round(prob, 3)
    except Exception:
        return 0.5


def _load_map_win_data(db) -> tuple[list, list, list]:
    """
    Load (hero_vec, map_name, win) triples for map-win model training.
    Returns (X_list, map_names, y_list).
    """
    vocab = _all_heroes(db)
    if not vocab:
        return [], [], []
    try:
        rows = db.execute(
            "SELECT dm.id, dm.map_name, dm.result, dm.team_name FROM de_maps dm "
            "WHERE dm.result IN ('Win','Loss')"
        ).fetchall()
        action_rows = db.execute(
            "SELECT de_map_id, hero FROM de_draft_actions WHERE action_type='pick'"
        ).fetchall()
    except Exception:
        return [], [], []

    from collections import defaultdict
    picks_by_map: dict[int, list] = defaultdict(list)
    for r in action_rows:
        picks_by_map[r["de_map_id"]].append(r["hero"])

    all_maps = sorted({r["map_name"] for r in rows if r["map_name"]})
    map_to_idx = {m: i for i, m in enumerate(all_maps)}
    X, map_names, y = [], [], []
    for r in rows:
        heroes = picks_by_map.get(r["id"], [])
        if not heroes:
            continue
        vec = _hero_vector(heroes, vocab)
        X.append(vec)
        map_names.append(r["map_name"])
        y.append(map_to_idx.get(r["map_name"], 0))
    return X, all_maps, y


def _fit_xgb_map(db):
    """Fit XGBoost map-selection classifier. Returns (model, map_labels) or (None, [])."""
    try:
        import xgboost as xgb
    except ImportError:
        return None, []

    X_list, map_labels, y_list = _load_map_win_data(db)
    if len(X_list) < 10 or len(set(y_list)) < 2:
        return None, map_labels

    X = np.vstack(X_list).astype(np.float32)
    y = np.array(y_list, dtype=np.int32)
    n_classes = len(map_labels)
    dtrain = xgb.DMatrix(X, label=y)
    params = {
        "objective": "multi:softprob",
        "num_class": n_classes,
        "eval_metric": "mlogloss",
        "max_depth": 3,
        "eta": 0.2,
        "subsample": 0.8,
        "seed": 42,
        "verbosity": 0,
    }
    model = xgb.train(params, dtrain, num_boost_round=40, verbose_eval=False)
    return model, map_labels


def predict_map_win_probs(
    our_heroes: list[str],
    db,
    top_n: int = 4,
) -> list[dict]:
    """
    XGBoost: given our current hero picks, return maps ranked by predicted win probability.
    Returns list of {map_name, probability}.
    """
    vocab = _all_heroes(db)
    if not vocab:
        return []

    cache_key = _cache_key("xgb_map", "global")
    cached = _cache_get(cache_key)
    if cached is None:
        model, labels = _fit_xgb_map(db)
        cached = (model, labels)
        _cache_set(cache_key, cached)

    model, labels = cached
    if model is None or not labels:
        return []

    try:
        import xgboost as xgb
        vec = _hero_vector(our_heroes, vocab).reshape(1, -1).astype(np.float32)
        dtest = xgb.DMatrix(vec)
        probs = model.predict(dtest)[0]  # shape (n_classes,)
        ranked = sorted(enumerate(probs), key=lambda x: -x[1])
        return [
            {"map_name": labels[i], "probability": round(float(p), 3)}
            for i, p in ranked[:top_n]
            if i < len(labels)
        ]
    except Exception:
        return []


def _load_ban_priority_data(db, enemy_team_name: str, season: str) -> list[dict]:
    """
    Build ban priority from de_team_hero_bias for the enemy team:
    score = played_count * win_rate + ban_count_weight.
    """
    try:
        if season and season.lower() != "all":
            rows = db.execute(
                "SELECT hero, ban_count, played_count, played_wins, played_losses "
                "FROM de_team_hero_bias WHERE team_name=? AND season=?",
                (enemy_team_name, season),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT hero, SUM(ban_count) AS ban_count, SUM(played_count) AS played_count, "
                "SUM(played_wins) AS played_wins, SUM(played_losses) AS played_losses "
                "FROM de_team_hero_bias WHERE team_name=? GROUP BY hero",
                (enemy_team_name,),
            ).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]


def _fit_xgb_ban(hero_rows: list[dict]):
    """
    XGBoost regression to score ban priority per hero from comfort/win-rate features.
    Returns trained model or None.
    """
    try:
        import xgboost as xgb
    except ImportError:
        return None

    if len(hero_rows) < 5:
        return None

    X, y = [], []
    for r in hero_rows:
        played = max(r.get("played_count") or 0, 1)
        wins = r.get("played_wins") or 0
        losses = r.get("played_losses") or 0
        bans = r.get("ban_count") or 0
        total = wins + losses
        wr = wins / total if total else 0.5
        comfort = played / max(sum(rr.get("played_count", 0) for rr in hero_rows), 1)
        # Target: a composite "threat" score we want to predict/sort
        threat = comfort * wr + bans * 0.1
        X.append([played, wins, losses, bans, wr, comfort])
        y.append(threat)

    X_arr = np.array(X, dtype=np.float32)
    y_arr = np.array(y, dtype=np.float32)
    dtrain = xgb.DMatrix(X_arr, label=y_arr)
    params = {
        "objective": "reg:squarederror",
        "max_depth": 3,
        "eta": 0.3,
        "subsample": 0.8,
        "seed": 42,
        "verbosity": 0,
    }
    model = xgb.train(params, dtrain, num_boost_round=30, verbose_eval=False)
    return model


def predict_ban_priority(
    enemy_team_name: str,
    db,
    season: str = "all",
    top_n: int = 6,
) -> list[dict]:
    """
    XGBoost: rank enemy heroes by ban priority using comfort + win-rate features.
    Returns list of {hero, threat_score, win_rate, played_count}.
    """
    hero_rows = _load_ban_priority_data(db, enemy_team_name, season)
    if not hero_rows:
        return []

    cache_key = _cache_key("xgb_ban", enemy_team_name, season)
    model = _cache_get(cache_key)
    if model is None:
        model = _fit_xgb_ban(hero_rows)
        _cache_set(cache_key, model)

    if model is None:
        # Fallback: sort by raw comfort score
        results = []
        total_played = sum(r.get("played_count", 0) for r in hero_rows) or 1
        for r in hero_rows:
            played = r.get("played_count") or 0
            wins = r.get("played_wins") or 0
            total = wins + (r.get("played_losses") or 0)
            wr = round(wins / total * 100, 1) if total else 0.0
            results.append({
                "hero": r["hero"],
                "threat_score": round(played / total_played, 4),
                "win_rate": wr,
                "played_count": played,
            })
        results.sort(key=lambda x: -x["threat_score"])
        return results[:top_n]

    try:
        import xgboost as xgb
        X = np.array([
            [
                r.get("played_count") or 0,
                r.get("played_wins") or 0,
                r.get("played_losses") or 0,
                r.get("ban_count") or 0,
                (r.get("played_wins") or 0) / max((r.get("played_wins") or 0) + (r.get("played_losses") or 0), 1),
                (r.get("played_count") or 0) / max(sum(rr.get("played_count", 0) for rr in hero_rows), 1),
            ]
            for r in hero_rows
        ], dtype=np.float32)
        dtest = xgb.DMatrix(X)
        scores = model.predict(dtest)
        results = []
        for i, r in enumerate(hero_rows):
            played = r.get("played_count") or 0
            wins = r.get("played_wins") or 0
            total = wins + (r.get("played_losses") or 0)
            wr = round(wins / total * 100, 1) if total else 0.0
            results.append({
                "hero": r["hero"],
                "threat_score": round(float(scores[i]), 4),
                "win_rate": wr,
                "played_count": played,
            })
        results.sort(key=lambda x: -x["threat_score"])
        return results[:top_n]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Site-context NLG
# ---------------------------------------------------------------------------

def _nlg_site_context(message: str, site_context_text: str, personal_team: str) -> str | None:
    if not site_context_text or not site_context_text.strip():
        return None
    lines = [ln.strip() for ln in site_context_text.splitlines() if ln.strip()]
    if not lines:
        return None

    player_lines = [ln for ln in lines if ln.startswith("Player ")]
    hero_lines = [ln for ln in lines if ln.startswith("Hero ")]
    team_lines = [ln for ln in lines if ln.startswith("Team ")]
    map_lines = [ln for ln in lines if ln.startswith("Map ")]
    scrim_lines = [ln for ln in lines if ln.startswith("Scrim ")]

    if player_lines:
        player_team = ""
        first_player_line = player_lines[0][len("Player "):]
        team_match = re.search(r"\bon\s+([^:(]+)", first_player_line)
        if team_match:
            player_team = (team_match.group(1) or "").strip()
        parts = [f"**{player_team or personal_team or 'Our team'} player data:**"]
        for ln in player_lines[:5]:
            parts.append(f"- {ln[len('Player '):]}")
        if hero_lines:
            parts.append("\n**Hero stats:**")
            for ln in hero_lines[:4]:
                parts.append(f"- {ln[len('Hero '):]}")
        return "\n".join(parts)

    if team_lines:
        parts = []
        for ln in team_lines[:2]:
            team_section = ln[len("Team "):]
            name_end = team_section.find(":")
            t_name = team_section[:name_end].strip() if name_end > 0 else team_section[:24]
            parts.append(f"**{t_name} snapshot:**")
            rest = team_section[name_end + 1:].strip() if name_end > 0 else team_section
            bias_m = re.search(r"bias \[(.+?)\]", rest)
            if bias_m:
                parts.append(f"- Hero bias: {bias_m.group(1)}")
            pairs_m = re.search(r"Pairs: (.+?)(?:\.|Maps:|$)", rest)
            if pairs_m:
                parts.append(f"- Core pairs: {pairs_m.group(1).strip()}")
            maps_m = re.search(r"Maps: (.+?)$", rest)
            if maps_m:
                parts.append(f"- Map record: {maps_m.group(1).strip()}")
        pool_lines = [ln for ln in lines if ln.startswith("  - ")]
        if pool_lines:
            parts.append("\n**Player pool:**")
            for ln in pool_lines[:8]:
                parts.append(ln)
        if scrim_lines:
            parts.append(f"\n**Recent scrims ({len(scrim_lines)}):**")
            for ln in scrim_lines[:5]:
                parts.append(f"- {ln[len('Scrim '):]}")
        return "\n".join(parts)

    if hero_lines:
        return "**Hero breakdown:**\n" + "\n".join(f"- {ln[len('Hero '):]}" for ln in hero_lines[:6])

    if map_lines:
        return "**Map breakdown:**\n" + "\n".join(f"- {ln[len('Map '):]}" for ln in map_lines[:6])

    if scrim_lines:
        parts = [f"**Scrim history ({len(scrim_lines)} entries):**"]
        for ln in scrim_lines[:8]:
            parts.append(f"- {ln[len('Scrim '):]}")
        return "\n".join(parts)

    return "\n".join(f"- {ln}" for ln in lines[:6])


# ---------------------------------------------------------------------------
# Draft matchup NLG — all four ML layers injected
# ---------------------------------------------------------------------------

def _join(lst: list, n: int = 4) -> str:
    seen: set[str] = set()
    result = []
    for item in lst:
        key = (item or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(item.strip())
        if len(result) >= n:
            break
    return ", ".join(result)


def _top_pair_text(rules: list[dict], n: int = 3) -> str:
    parts = []
    for r in rules[:n]:
        parts.append(f"{r['hero_a']} + {r['hero_b']} ({r['win_rate']}% WR, {r['co_appearances']}g)")
    return "; ".join(parts)


def _nlg_matchup(
    message: str,
    context_text: str,
    meta: dict,
    intent: str,
    personal_team: str,
    db,
    season: str,
) -> str:
    visuals = meta.get("visuals") or {}
    ban_heroes = visuals.get("recommended_bans") or []
    protect_heroes = visuals.get("recommended_protects") or []
    comp_heroes = visuals.get("target_comp") or []
    our_comfort = visuals.get("our_comfort") or []
    enemy_comfort = visuals.get("enemy_comfort") or []
    contested = visuals.get("contested") or []
    volatile_rows = visuals.get("volatile_rows") or []
    enemy_comps = visuals.get("enemy_comps") or []
    pivot_predictions = visuals.get("pivot_predictions") or []
    our_comp_rows = visuals.get("our_comp_rows") or []

    team_a = meta.get("team_a") or personal_team or "Us"
    team_b = meta.get("team_b") or "Them"

    bans = _join(ban_heroes, 4)
    protects = _join(protect_heroes, 3)
    comp = _join(comp_heroes, 6)
    our_line = _join(our_comfort, 4)
    enemy_line = _join(enemy_comfort, 4)
    contested_line = _join(contested, 4)
    volatile_line = ", ".join(
        f"{r.get('hero')} ({r.get('favored_side')}, Δ{r.get('delta', 0)})"
        for r in volatile_rows[:3] if r.get("hero")
    ) or _join(visuals.get("volatile") or [], 3)

    # --- ML layer 1: Association rules ---
    our_rules = mine_association_rules(team_a, db, season, is_enemy=False, top_n=6) if db else []
    enemy_rules = mine_association_rules(team_b, db, season, is_enemy=True, top_n=6) if db else []
    our_synergy = _top_pair_text(our_rules, 3)
    enemy_synergy = _top_pair_text(enemy_rules, 3)

    # --- ML layer 2: K-Means comp archetypes ---
    our_clusters = cluster_comps(team_a, db, season, n_clusters=4) if db else []
    enemy_clusters = cluster_comps(team_b, db, season, n_clusters=4) if db else []
    our_archetype = our_clusters[0]["archetype"] if our_clusters else ""
    enemy_archetype = enemy_clusters[0]["archetype"] if enemy_clusters else ""

    # --- ML layer 3: Naive Bayes next pick (enemy) ---
    enemy_next = predict_next_pick(team_b, list(enemy_comfort[:2]), db, season, top_n=3, is_enemy=True) if db else []
    nb_next = _join([r["hero"] for r in enemy_next], 3)

    # --- ML layer 4a: XGBoost win probability ---
    win_prob = predict_win(list(comp_heroes[:6]), list(enemy_comfort[:6]), db) if db else 0.5
    win_pct = round(win_prob * 100, 1)

    # --- ML layer 4b: XGBoost map recommendations ---
    map_preds = predict_map_win_probs(list(comp_heroes[:6]), db, top_n=3) if db else []
    ml_maps = _join([r["map_name"] for r in map_preds], 3)

    # --- ML layer 4c: XGBoost ban priority ---
    ban_preds = predict_ban_priority(team_b, db, season, top_n=5) if db else []
    xgb_bans = _join([r["hero"] for r in ban_preds], 4)

    # ---- Intent dispatch ----

    if intent == "ban":
        ban_seq = [r["hero"] for r in ban_preds[:4] if r.get("hero")] if ban_preds else list((ban_heroes or [])[:4])
        seq_line = " | ".join(f"Ban {i+1}: **{h}**" for i, h in enumerate(ban_seq))
        lines = [f"Based on their draft history, the top four bans are {xgb_bans or bans}."]
        if seq_line:
            lines.append(f"Likely sequence: {seq_line}.")
        lines.append("If you tell me your exact first ban, I can re-rank this list for that scenario.")
        if enemy_synergy:
            lines.append(f"The pairings I would prioritize breaking are: {enemy_synergy}.")
        if nb_next:
            lines.append(f"Their next-pick pressure points are {nb_next}, so keep that in mind for follow-up bans.")
        if comp:
            lines.append(f"That should leave your route open on {comp}.")
        return "\n".join(lines)

    elif intent == "protect":
        lines = [f"Lock protects on **{protects or our_line}**."]
        if our_synergy:
            lines.append(f"Our highest-lift pairs: {our_synergy}.")
        if our_archetype:
            lines.append(f"Our primary archetype is **{our_archetype}** — protect the core of that.")
        if comp:
            lines.append(f"Keeps **{comp}** live.")
        return "\n".join(lines)

    elif intent == "comp":
        lines = []
        if our_clusters:
            lines.append(f"**{team_a}** comp archetypes (K-Means):")
            for c in our_clusters[:3]:
                lines.append(f"- {c['archetype']}: {_join(c['top_heroes'], 5)} ({c['win_rate']}% WR, {c['count']} comps)")
        elif our_comp_rows:
            lines.append(f"Best comp options for **{team_a}**:")
            for i, r in enumerate(our_comp_rows[:4], 1):
                lines.append(f"{i}. {_join(r.get('heroes', []), 6)} — {r.get('win_rate', 0)}% WR")
        else:
            lines.append(f"Lean into **{comp or our_line}**.")
        if our_synergy:
            lines.append(f"\nTop synergy pairs: {our_synergy}.")
        if win_pct != 50.0:
            lines.append(f"Win probability for this comp path: **{win_pct}%** (XGBoost).")
        return "\n".join(lines)

    elif intent == "map":
        lines = []
        if map_preds:
            lines.append(f"**XGBoost map predictions** for {team_a}'s comp:")
            for r in map_preds[:3]:
                lines.append(f"- {r['map_name']} ({round(r['probability']*100, 1)}% predicted win rate)")
        map_rows = visuals.get("map_consensus") or []
        if map_rows:
            row0 = map_rows[0] if map_rows else {}
            best = _join(row0.get("maps", []), 3)
            lines.append(f"\nHistorical consensus: **{best}**.")
        if pivot_predictions:
            p = pivot_predictions[0]
            pivot_h = _join(p.get("pivot", []), 4)
            counter = _join(p.get("counter", []), 4)
            if pivot_h:
                lines.append(f"If they pivot to {pivot_h}, answer with {counter or comp}.")
        return "\n".join(lines) if lines else f"Not enough map data yet for {team_a} vs {team_b}."

    elif intent == "risk":
        lines = [f"Swing pieces: **{volatile_line or contested_line}**."]
        if xgb_bans:
            lines.append(f"XGBoost ban priority removes the highest-threat swing: **{xgb_bans}**.")
        if win_pct != 50.0:
            lines.append(f"Current comp path win probability: **{win_pct}%**.")
        return "\n".join(lines)

    elif intent == "comfort":
        lines = [f"**{team_a}** archetype: {our_archetype or 'mixed'} — comfort: {our_line or 'not clear yet'}."]
        lines.append(f"**{team_b}** archetype: {enemy_archetype or 'mixed'} — comfort: {enemy_line or 'not clear yet'}.")
        if our_synergy:
            lines.append(f"\nOur top pairs: {our_synergy}.")
        if enemy_synergy:
            lines.append(f"Their top pairs: {enemy_synergy}.")
        return "\n".join(lines)

    elif intent == "contested":
        lines = [f"The draft fight centers on **{contested_line or bans}**."]
        if xgb_bans:
            lines.append(f"XGBoost puts their highest-threat pieces at: {xgb_bans}.")
        if bans:
            lines.append(f"To avoid the mirror, ban **{bans}** and take **{comp}**.")
        return "\n".join(lines)

    elif intent == "enemy_comps":
        lines = []
        if enemy_clusters:
            lines.append(f"**{team_b}** comp archetypes (K-Means):")
            for c in enemy_clusters[:3]:
                lines.append(f"- {c['archetype']}: {_join(c['top_heroes'], 5)} ({c['win_rate']}% WR)")
        elif enemy_comps:
            top = enemy_comps[0]
            lines.append(f"**{team_b}**'s cleanest look: **{_join(top.get('heroes', []), 6)}** ({top.get('win_rate', 0)}% WR).")
        if enemy_line or our_line:
            lines.append(f"\nComfort read: {team_b} leans on {enemy_line or 'their comfort core'}, and we can anchor on {our_line or 'our comfort core' }.")
        if enemy_synergy:
            lines.append(f"\nKey synergy pairs: {enemy_synergy}.")
        if nb_next:
            lines.append(f"NB predicts next pick: **{nb_next}**.")
        if xgb_bans:
            lines.append(f"Break it up with: **{xgb_bans}**.")
        return "\n".join(lines) if lines else f"Not enough data on {team_b} comps yet."

    elif intent == "next_pick":
        lines = []
        if enemy_next:
            lines.append(f"**{team_b}** most likely next picks (Naive Bayes):")
            for r in enemy_next[:3]:
                lines.append(f"- {r['hero']} ({round(r['probability']*100, 1)}%)")
        else:
            lines.append(f"No strong NB signal — watch {enemy_line or 'their comfort core'}.")
        return "\n".join(lines)

    elif intent == "pivot":
        if pivot_predictions:
            p = pivot_predictions[0]
            base = _join(p.get("base", []), 4)
            pivot_h = _join(p.get("pivot", []), 4)
            counter = _join(p.get("counter", []), 4)
            lines = [f"If **{team_b}** opens on {base}, expect pivot to **{pivot_h}**."]
            lines.append(f"Our counter: **{counter or comp}**.")
            if nb_next:
                lines.append(f"NB also predicts: {nb_next}.")
            return "\n".join(lines)
        return (
            f"No strong pivot signal yet. Watch **{enemy_line or 'their comfort core'}** and "
            f"stay ready to answer with **{bans or comp}**."
        )

    elif intent == "ban_impact":
        message_l = (message or "").lower()
        hero = ""
        for h in _all_heroes(db) if db else []:
            h_key = (h or "").strip().lower()
            if h_key and re.search(rf"(?<![a-z0-9]){re.escape(h_key)}(?![a-z0-9])", message_l):
                hero = h
                break
        if not hero:
            hero_match = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b", message or "")
            hero = hero_match.group(1) if hero_match else "that hero"
        hero_key = hero.lower()
        filtered_comp = _join([h for h in comp_heroes if h.lower() != hero_key], 5)
        filtered_bans = _join([h for h in ban_heroes if h.lower() != hero_key], 4)
        ban_seq = [r["hero"] for r in ban_preds if r.get("hero", "").lower() != hero_key][:4]
        if not ban_seq:
            ban_seq = [h for h in (ban_heroes or []) if (h or "").lower() != hero_key][:4]
        likely_enemy_bans = _join(ban_seq, 4)
        lines = [f"If we first-ban {hero}, {team_b}'s likely ban board becomes {likely_enemy_bans or filtered_bans or xgb_bans or enemy_line}."]
        if ban_seq:
            lines.append("Likely sequence: " + " | ".join(f"Ban {i+1}: **{h}**" for i, h in enumerate(ban_seq)) + ".")
        if enemy_line or our_line:
            lines.append(f"Comfort read after that ban: they still lean on {enemy_line or 'their comfort core'}, while we can route through {our_line or 'our comfort core'}." )
        lines.append(f"That usually shifts the draft toward {filtered_comp or enemy_line or 'their next best comfort lane'}.")
        if nb_next:
            lines.append(f"After that, the next-pick pressure is around {nb_next}.")
        return "\n".join(lines)

    elif intent == "confidence":
        conf = visuals.get("confidence") or {}
        return (
            f"Model leans: **{comp or our_line}**.\n"
            f"Historical confidence: {conf.get('confidence', 0)}% across {conf.get('sample', 0)} records.\n"
            f"XGBoost win probability: **{win_pct}%**."
        )

    elif intent == "stats":
        conf = visuals.get("confidence") or {}
        lines = [f"**{team_a}** vs **{team_b}** — {conf.get('sample', 0)} filtered records."]
        lines.append(f"XGBoost win probability: **{win_pct}%**.")
        if our_synergy:
            lines.append(f"Our top synergy pairs: {our_synergy}.")
        if enemy_synergy:
            lines.append(f"Their top synergy pairs: {enemy_synergy}.")
        return "\n".join(lines)

    else:
        # General / check / default — fire all layers
        lines = [f"Open on **{xgb_bans or bans or enemy_line}** (XGBoost ban priority)."]
        if nb_next:
            lines.append(f"NB predicts their next pick: **{nb_next}**.")
        if our_archetype or enemy_archetype:
            lines.append(
                f"Matchup shape: {team_a} ({our_archetype or 'mixed'}) vs {team_b} ({enemy_archetype or 'mixed'})."
            )
        if win_pct != 50.0:
            lines.append(f"Win probability on **{comp or our_line}**: **{win_pct}%**.")
        if our_synergy:
            lines.append(f"Our top pairs: {our_synergy}.")
        if volatile_line:
            lines.append(f"Main swing: {volatile_line}.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_local_answer(
    message: str,
    context_text: str,
    site_context_text: str,
    meta: dict,
    intent: str,
    personal_team: str,
    db=None,
    season: str = "all",
) -> str:
    """
    Generate a natural-language draft answer using only local ML.

    Priority:
      1. Valid matchup → matchup NLG with all four ML layers.
      2. Site-context query → structured NLG from snapshot data.
      3. Generic prompt for more info.
    """
    if meta and meta.get("has_matchup"):
        return _nlg_matchup(message, context_text, meta, intent, personal_team, db, season)

    if site_context_text:
        result = _nlg_site_context(message, site_context_text, personal_team)
        if result:
            return result

    return (
        "I couldn't find enough data to answer that. "
        "Try naming a specific team, player, hero, map, or season — "
        "for example: `Virtus Pro snapshot season 7` or `Dr. Strange profile`."
    )

