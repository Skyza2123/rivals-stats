"""
sequential_predictor.py — Sequential probability model for Marvel Rivals drafts.

DESIGN:
  Model draft prediction as a sequence problem: given current draft state
  (bans, picks, side, pick order), predict P(next_hero) for each available hero.
  
  All probabilities are derived from historical draft data only. No theory,
  no LLM reasoning — just frequencies and co-occurrence patterns.

DATA SOURCES:
  - de_draft_actions       — sequential order of bans, protects, picks
  - de_team_hero_bias      — per-team hero tendencies (baseline priors)
  - de_ally_pair_stats     — co-occurrence patterns within teams
  - de_enemy_pair_stats    — co-occurrence patterns across enemy teams

ALGORITHM:
  For each candidate hero:
  1. Base probability: how often this hero was picked/banned in similar slot
     (sourced from de_draft_actions, normalized by pick order)
  2. Team bias weight: team's tendency to pick/ban this hero
     (sourced from de_team_hero_bias)
  3. Co-occurrence boost: how often this hero appeared with already-picked heroes
     (sourced from de_ally_pair_stats)
  4. Normalize across all candidates → P(next_hero)
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from typing import Optional


class SequentialDraftPredictor:
    """
    Predict the next pick/ban given a draft state.
    
    Attributes:
      db_path: Path to SQLite database with draft_engine tables
      team_name: Team whose hero pool we're predicting for (for tendencies)
      enemy_team_name: Opponent team (for enemy pair stats context)
    """
    
    def __init__(self, db_path: str, team_name: Optional[str] = None):
        self.db_path = db_path
        self.team_name = team_name
        self.conn = None
        
    def connect(self):
        """Open database connection."""
        if not self.conn:
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row
            
    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None
            
    def predict_next_pick(
        self,
        current_bans: list[str],
        current_picks: list[str],
        team_slot: str,
        pick_order: int,
        available_heroes: list[str],
        season: Optional[str] = None,
    ) -> dict[str, float]:
        """
        Predict P(next_hero) given draft state.
        
        Args:
          current_bans: List of heroes banned so far (by either team)
          current_picks: List of heroes picked so far (by either team)
          team_slot: 'team1' or 'team2' (whose turn it is)
          pick_order: Pick number (1-based) in the draft sequence
          available_heroes: Pool of legal heroes (excluding banned/picked)
          season: Season filter (optional)
          
        Returns:
          Dict mapping hero_name → probability (normalized to sum to 1.0)
        """
        self.connect()
        try:
            # Ensure we have valid hero data
            used_heroes = set(current_bans) | set(current_picks)
            candidates = [h for h in available_heroes if h not in used_heroes]
            if not candidates:
                return {}
            
            # Fetch base distributions for this pick slot
            slot_distributions = self._get_slot_distributions(pick_order, team_slot, season)
            
            # Fetch team bias for this team
            team_bias = {}
            if self.team_name:
                team_bias = self._get_team_hero_bias(self.team_name, season)
            
            # For each candidate, compute combined score
            scores = {}
            for hero in candidates:
                score = self._score_candidate(
                    hero,
                    current_picks,
                    slot_distributions,
                    team_bias,
                    pick_order,
                    season,
                )
                if score > 0:
                    scores[hero] = score
            
            # Normalize to probability distribution
            if not scores:
                # Fallback: uniform over candidates
                uniform_prob = 1.0 / len(candidates)
                return {hero: uniform_prob for hero in candidates}
            
            total = sum(scores.values())
            return {hero: (score / total) for hero, score in scores.items()}
            
        finally:
            self.close()
    
    def _get_slot_distributions(
        self, pick_order: int, team_slot: str, season: Optional[str] = None
    ) -> dict[str, int]:
        """
        Fetch how often each hero was picked/banned in this exact slot.
        
        Returns: {hero_name: count}
        """
        cursor = self.conn.cursor()
        
        query = """
        SELECT hero, COUNT(*) as cnt
        FROM de_draft_actions
        WHERE action_order = ?
          AND team_slot = ?
          {season_filter}
        GROUP BY hero
        """
        
        params = [pick_order, team_slot]
        if season:
            query = query.replace("{season_filter}", "AND season = ?")
            params.append(season)
        else:
            query = query.replace("{season_filter}", "")
        
        cursor.execute(query, params)
        return {row["hero"]: row["cnt"] for row in cursor.fetchall()}
    
    def _get_team_hero_bias(
        self, team_name: str, season: Optional[str] = None
    ) -> dict[str, float]:
        """
        Fetch team's baseline tendency to pick each hero.
        
        Combines ban_count, protect_count, and played_count into a single
        "affinity" score per hero for this team.
        
        Returns: {hero_name: affinity_weight}
        """
        cursor = self.conn.cursor()
        
        query = """
        SELECT hero,
               (ban_count + protect_count + played_count) as total_affinity
        FROM de_team_hero_bias
        WHERE team_name = ?
          {season_filter}
        """
        
        params = [team_name]
        if season:
            query = query.replace("{season_filter}", "AND season = ?")
            params.append(season)
        else:
            query = query.replace("{season_filter}", "")
        
        cursor.execute(query, params)
        return {row["hero"]: float(row["total_affinity"]) for row in cursor.fetchall()}
    
    def _get_ally_pair_stats(
        self, hero: str, season: Optional[str] = None
    ) -> dict[str, int]:
        """
        Fetch how often `hero` appeared with other heroes on our team.
        
        Returns: {co_hero: co_appearance_count}
        """
        if not self.team_name:
            return {}
        
        cursor = self.conn.cursor()
        
        query = """
        SELECT CASE 
                 WHEN hero_a = ? THEN hero_b
                 ELSE hero_a
               END as co_hero,
               co_appearances
        FROM de_ally_pair_stats
        WHERE team_name = ?
          AND (hero_a = ? OR hero_b = ?)
          {season_filter}
        """
        
        params = [hero, self.team_name, hero, hero]
        if season:
            query = query.replace("{season_filter}", "AND season = ?")
            params.append(season)
        else:
            query = query.replace("{season_filter}", "")
        
        cursor.execute(query, params)
        return {row["co_hero"]: row["co_appearances"] for row in cursor.fetchall()}
    
    def _score_candidate(
        self,
        hero: str,
        current_picks: list[str],
        slot_distributions: dict[str, int],
        team_bias: dict[str, float],
        pick_order: int,
        season: Optional[str] = None,
    ) -> float:
        """
        Score a candidate hero using:
        1. Slot distribution baseline
        2. Team tendency bias
        3. Co-occurrence with current picks
        4. Laplace smoothing
        """
        alpha = 1.0  # Smoothing constant
        
        # 1. Base probability from slot distribution
        slot_count = slot_distributions.get(hero, 0)
        total_slot = sum(slot_distributions.values())
        if not total_slot:
            # No historical data for this slot; use uniform
            base_score = 1.0
        else:
            base_score = (slot_count + alpha) / (total_slot + alpha * 100)
        
        # 2. Team bias multiplier
        team_affinity = team_bias.get(hero, 1.0)
        if team_affinity > 0:
            bias_multiplier = math.log(team_affinity + 1)  # Log to avoid extreme scaling
        else:
            bias_multiplier = 0.5
        
        # 3. Co-occurrence boost
        co_occurrence_boost = 1.0
        if current_picks:
            pair_stats = self._get_ally_pair_stats(hero, season)
            pair_scores = [pair_stats.get(p, 0) for p in current_picks]
            if pair_scores:
                avg_pair_count = sum(pair_scores) / len(pair_scores)
                co_occurrence_boost = 1.0 + math.log(max(1.0, avg_pair_count + 1))
        
        # Combine
        final_score = base_score * (1.0 + bias_multiplier) * co_occurrence_boost
        return max(0.0, final_score)


def build_sequential_predictor(
    db_path: str,
    team_name: Optional[str] = None,
) -> SequentialDraftPredictor:
    """Factory to build a sequential predictor."""
    return SequentialDraftPredictor(db_path, team_name)
