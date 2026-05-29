from __future__ import annotations

import math

import pandas as pd

from winstan.config import AppConfig
from winstan.outputs.explanations import get_trend_stage_label, get_watch_rank_label


DEFAULT_WATCH_WINDOW_DAYS = 3


def build_trade_watch_signal(
    row: pd.Series,
    config: AppConfig,
    watch_window_days: int = DEFAULT_WATCH_WINDOW_DAYS,
    watch_source: str = "stage2_auto",
) -> dict[str, object]:
    close = _to_float(row.get("close"))
    breakout_level = _to_float(row.get("breakout_level"))
    nearest_resistance = _to_float(row.get("nearest_resistance"))
    breakout_entry_price = _resolve_target_entry_price(row, config)
    pullback_entry_price = _resolve_pullback_entry_price(row)
    stop_loss_reference = _resolve_stop_loss_reference(row)
    latest_close = close
    distance_to_entry_pct = None
    distance_to_pullback_pct = None
    if latest_close is not None and breakout_entry_price is not None and latest_close != 0:
        distance_to_entry_pct = (breakout_entry_price / latest_close - 1.0) * 100.0
    if latest_close is not None and pullback_entry_price is not None and latest_close != 0:
        distance_to_pullback_pct = (pullback_entry_price / latest_close - 1.0) * 100.0

    volume_ratio = _to_float(row.get("volume_ratio"))
    return {
        "symbol": _to_text(row.get("symbol")).upper(),
        "name": _to_text(row.get("name")),
        "source_trade_date": _to_iso_date(row.get("trade_date")),
        "watch_date": _to_iso_date(row.get("trade_date")),
        "status": "watching",
        "watch_source": watch_source,
        "watch_window_days": int(max(watch_window_days, 1)),
        "target_entry_price": breakout_entry_price,
        "pullback_entry_price": pullback_entry_price,
        "breakout_level": breakout_level,
        "stop_loss_reference": stop_loss_reference,
        "volume_confirmation_needed": False,
        "volume_ratio_at_signal": volume_ratio,
        "volume_label": _volume_label(volume_ratio),
        "volume_ok_at_signal": _to_bool(row.get("volume_ok")),
        "stage_label": get_trend_stage_label(row, config),
        "watch_rank_label": get_watch_rank_label(row),
        "stage2_score": _to_float(row.get("stage2_score")),
        "final_score": _to_float(row.get("final_score")),
        "structure_score": _to_float(row.get("structure_score")),
        "timing_score": _to_float(row.get("timing_score")),
        "strength_score": _to_float(row.get("strength_score")),
        "risk_score": _to_float(row.get("risk_score")),
        "rs_rank_pct": _to_float(row.get("rs_rank_pct")),
        "headroom_pct": _to_float(row.get("headroom_pct")),
        "market_ok": _to_bool(row.get("market_ok")),
        "breakout_status": _to_text(row.get("breakout_status")) or "no_breakout_level",
        "latest_trade_date": _to_iso_date(row.get("trade_date")),
        "latest_close": latest_close,
        "distance_to_entry_pct": distance_to_entry_pct,
        "distance_to_pullback_pct": distance_to_pullback_pct,
        "days_waited": 0,
        "expire_date": "",
        "trigger_date": "",
        "trigger_price_observed": None,
        "trigger_mode": "",
        "volume_confirmed_on_trigger": False,
    }


def _resolve_target_entry_price(row: pd.Series, config: AppConfig) -> float | None:
    close = _to_float(row.get("close"))
    breakout_level = _to_float(row.get("breakout_level"))
    nearest_resistance = _to_float(row.get("nearest_resistance"))
    breakout_status = _to_text(row.get("breakout_status")) or "no_breakout_level"
    confirm_step = (close or breakout_level or nearest_resistance or 0.0) * min(config.strategy.breakout_min_pct, 2.0) / 100.0

    if breakout_status == "just_broke_out":
        if nearest_resistance is not None and close is not None and nearest_resistance > close:
            return round(nearest_resistance, 4)
        if close is not None:
            return round(close + max(confirm_step, 0.01), 4)

    if breakout_level is not None:
        if close is None or breakout_level >= close:
            return round(breakout_level, 4)
        return round(close + max(confirm_step, 0.01), 4)

    if nearest_resistance is not None and close is not None and nearest_resistance > close:
        return round(nearest_resistance, 4)

    if close is not None:
        return round(close + max(confirm_step, 0.01), 4)
    return None


def _resolve_pullback_entry_price(row: pd.Series) -> float | None:
    breakout_level = _to_float(row.get("breakout_level"))
    breakout_status = _to_text(row.get("breakout_status")) or "no_breakout_level"
    if breakout_status != "just_broke_out" or breakout_level is None:
        return None
    return round(breakout_level, 4)


def _resolve_stop_loss_reference(row: pd.Series) -> float | None:
    for key in ("breakout_level", "ma_10w", "ma_30w"):
        value = _to_float(row.get(key))
        if value is not None:
            return round(value, 4)
    return None


def _volume_label(volume_ratio: float | None) -> str:
    if volume_ratio is None:
        return "未知"
    if volume_ratio >= 1.8:
        return "明显放量"
    if volume_ratio >= 1.2:
        return "温和放量"
    if volume_ratio >= 0.9:
        return "量能一般"
    return "量能偏弱"


def _to_float(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(numeric):
        return None
    return numeric


def _to_bool(value: object) -> bool:
    if value is None or pd.isna(value):
        return False
    return bool(value)


def _to_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value)


def _to_iso_date(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.strftime("%Y-%m-%d")
