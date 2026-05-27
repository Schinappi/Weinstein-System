from __future__ import annotations

import pandas as pd


def evaluate_volume(recent: pd.DataFrame) -> dict[str, object]:
    if recent.empty:
        return {"volume_ok": False, "volume_score": 0.0, "volume_reason": "无数据"}

    latest = recent.iloc[-1]
    prior = recent.iloc[-2] if len(recent) >= 2 else latest
    avg_volume = latest.get("weekly_volume_ma_10")
    if pd.isna(avg_volume) or avg_volume == 0:
        return {"volume_ok": False, "volume_score": 0.0, "volume_reason": "历史量能不足"}

    latest_ratio = latest["volume"] / avg_volume
    pullback_quiet = bool(prior["volume"] <= prior.get("weekly_volume_ma_10", prior["volume"]))
    volume_ok = bool(latest_ratio >= 1.0 or pullback_quiet)
    volume_score = min(max((latest_ratio - 0.7) * 100.0, 0.0), 100.0)

    return {
        "volume_ok": volume_ok,
        "volume_score": volume_score,
        "volume_ratio": float(latest_ratio),
        "volume_reason": "量能支持趋势" if volume_ok else "量能确认偏弱",
    }
