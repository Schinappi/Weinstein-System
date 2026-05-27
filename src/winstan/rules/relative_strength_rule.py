from __future__ import annotations

import pandas as pd

from winstan.config import AppConfig


def evaluate_relative_strength(latest: pd.Series, config: AppConfig) -> dict[str, object]:
    rank_pct = latest.get("rs_rank_pct")
    rs_composite = latest.get("rs_composite")
    rs_line = latest.get("rs_line")
    rs_ok = bool(pd.notna(rank_pct) and rank_pct <= config.strategy.rs_rank_threshold_pct)
    rs_score = 0.0
    if pd.notna(rank_pct):
        rs_score = max(0.0, 100.0 - (rank_pct - 1.0))
    return {
        "rs_ok": rs_ok,
        "rs_score": rs_score,
        "rs_rank_pct": float(rank_pct) if pd.notna(rank_pct) else None,
        "rs_composite": float(rs_composite) if pd.notna(rs_composite) else None,
        "rs_line": float(rs_line) if pd.notna(rs_line) else None,
    }

