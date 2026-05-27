from __future__ import annotations

import pandas as pd

from winstan.config import AppConfig
from winstan.rules.stage_analysis import apply_stage2_scoring

WATCH_BREAKOUT_STATUSES = {"near_breakout", "just_broke_out"}
WATCH_STAGE1 = {"I"}
WATCH_STAGE2 = {"I", "II"}


def score_and_rank(results: pd.DataFrame, config: AppConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    if results.empty:
        return results.copy(), results.copy()

    weighted = apply_stage2_scoring(results, config)
    weights = config.ranking.weights
    weighted["resistance_score"] = weighted["headroom_pct"].fillna(config.strategy.resistance_min_headroom_pct).clip(0, 100)
    weighted["breakout_bonus_score"] = weighted["breakout_strength"].fillna(0.0).clip(0, 100)
    weighted["total_score"] = (
        weighted["trend_score"].fillna(0.0) * weights["trend"]
        + weighted["rs_score"].fillna(0.0) * weights["rs"]
        + weighted["volume_score"].fillna(0.0) * weights["volume"]
        + weighted["resistance_score"].fillna(0.0) * weights["resistance"]
        + weighted["breakout_bonus_score"].fillna(0.0) * weights["breakout_bonus"]
    )

    weighted["is_stage1_watch"] = weighted["stage_label"].eq("I")
    weighted["is_flat_base"] = weighted["base_flatness_ok"].fillna(False)
    weighted["is_just_broke_out"] = weighted["breakout_status"].eq("just_broke_out")
    weighted["is_near_breakout"] = weighted["breakout_status"].eq("near_breakout")
    weighted["is_extended"] = (
        weighted["breakout_status"].eq("extended_breakout")
        | weighted["price_vs_ma_pct"].fillna(0.0).gt(config.strategy.watch_max_price_vs_ma_pct)
    )
    weighted["watch_reason"] = weighted.apply(_build_watch_reason, axis=1)
    weighted["watch_score"] = (
        weighted["rs_score"].fillna(0.0) * 0.35
        + weighted["volume_score"].fillna(0.0) * 0.25
        + weighted["resistance_score"].fillna(0.0) * 0.10
        + weighted["trend_score"].fillna(0.0) * 0.10
        + weighted["is_stage1_watch"].astype(float) * 18.0
        + weighted["is_flat_base"].astype(float) * 14.0
        + weighted["is_just_broke_out"].astype(float) * 24.0
        + weighted["is_near_breakout"].astype(float) * 18.0
        - weighted["is_extended"].astype(float) * 40.0
    )

    candidate_mask = _build_stage2_candidate_mask(weighted)
    candidates = weighted[candidate_mask].sort_values(["total_score", "rs_rank_pct"], ascending=[False, True]).reset_index(drop=True)

    watch_mask = (
        weighted["stage_label"].isin(WATCH_STAGE1)
        & weighted["is_flat_base"]
        & ~weighted["is_extended"]
        & weighted["breakout_status"].isin(WATCH_BREAKOUT_STATUSES)
    )
    watch_pool = weighted[watch_mask].copy()
    if watch_pool.empty:
        watch_pool = weighted[
            weighted["stage_label"].isin(WATCH_STAGE1)
            & weighted["is_flat_base"]
            & ~weighted["is_extended"]
        ].copy()

    top_n = watch_pool.sort_values(
        ["watch_score", "rs_rank_pct", "breakout_pct"],
        ascending=[False, True, False],
        na_position="last",
    ).head(config.ranking.top_n).reset_index(drop=True)
    top_n["top_n_rank"] = range(1, len(top_n) + 1)
    return candidates, top_n


def build_stage2_top_n(results: pd.DataFrame, config: AppConfig) -> pd.DataFrame:
    if results.empty:
        return results.copy()

    weighted = apply_stage2_scoring(results, config)
    candidate_pool = weighted[_build_stage2_candidate_mask(weighted)].copy()
    stage2_top_n = candidate_pool.sort_values(
        ["final_score", "structure_score", "timing_score", "strength_score", "rs_rank_pct", "headroom_pct"],
        ascending=[False, False, False, False, True, False],
        na_position="last",
    ).head(config.ranking.stage2_top_n).reset_index(drop=True)
    stage2_top_n["stage2_top_n_rank"] = range(1, len(stage2_top_n) + 1)
    return stage2_top_n


def build_quasi_stage2_top_n(results: pd.DataFrame, config: AppConfig) -> pd.DataFrame:
    if results.empty:
        return results.copy()

    weighted = apply_stage2_scoring(results, config)
    strict_mask = _build_stage2_candidate_mask(weighted)
    core_columns = ["stage2_candidate", "volume_ok", "rs_ok", "resistance_ok", "breakout_ok"]
    core_pass_count = weighted[core_columns].fillna(False).astype(bool).sum(axis=1)
    quasi_pool = weighted[
        weighted["market_ok"].fillna(False).astype(bool)
        & weighted["stage_label"].isin(WATCH_STAGE2)
        & ~strict_mask
        & core_pass_count.ge(3)
    ].copy()
    if quasi_pool.empty:
        return quasi_pool

    quasi_pool["quasi_stage2_rank_score"] = core_pass_count.loc[quasi_pool.index]
    quasi_top_n = quasi_pool.sort_values(
        [
            "quasi_stage2_rank_score",
            "final_score",
            "structure_score",
            "timing_score",
            "strength_score",
            "rs_rank_pct",
            "headroom_pct",
        ],
        ascending=[False, False, False, False, False, True, False],
        na_position="last",
    ).head(config.ranking.stage2_top_n).reset_index(drop=True)
    quasi_top_n["quasi_stage2_rank"] = range(1, len(quasi_top_n) + 1)
    return quasi_top_n


def _build_stage2_candidate_mask(weighted: pd.DataFrame) -> pd.Series:
    return (
        weighted["market_ok"].fillna(False).astype(bool)
        & weighted["stage2_candidate"].fillna(False).astype(bool)
        & weighted["volume_ok"].fillna(False).astype(bool)
        & weighted["rs_ok"].fillna(False).astype(bool)
        & weighted["breakout_ok"].fillna(False).astype(bool)
    )


def _build_watch_reason(row: pd.Series) -> str:
    reasons: list[str] = []
    if bool(row.get("is_stage1_watch", False)):
        reasons.append("阶段I观察")
    if bool(row.get("is_flat_base", False)):
        reasons.append("平整基底")
    if bool(row.get("is_near_breakout", False)):
        reasons.append("临近突破")
    if bool(row.get("is_just_broke_out", False)):
        reasons.append("刚突破")
    if not reasons:
        reasons.append("趋势观察")
    return " / ".join(reasons)
