from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from winstan.config import AppConfig
from winstan.outputs.explanations import get_trend_stage_label


def export_results(
    config: AppConfig,
    results: pd.DataFrame,
    candidates: pd.DataFrame,
    top_n: pd.DataFrame,
    stage2_top_n: pd.DataFrame,
    summary: dict[str, object],
) -> None:
    config.reports_dir.mkdir(parents=True, exist_ok=True)

    if config.output.export_candidates:
        candidates.to_csv(config.reports_dir / "candidates.csv", index=False, encoding="utf-8-sig")

    if config.output.export_top_n:
        _safe_write_csv(
            _format_top_n_for_reading(top_n, config),
            config.reports_dir / "top_n.csv",
            config.reports_dir / "top_n_stage1.csv",
        )
        _safe_write_csv(
            _format_stage2_top_n_for_reading(stage2_top_n, config),
            config.reports_dir / "stage2_top10.csv",
            config.reports_dir / "stage2_top10_fallback.csv",
        )

    if config.output.export_summary:
        (config.reports_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if config.output.export_debug:
        results.to_csv(config.reports_dir / "debug.csv", index=False, encoding="utf-8-sig")


def _format_top_n_for_reading(top_n: pd.DataFrame, config: AppConfig) -> pd.DataFrame:
    if top_n.empty:
        return pd.DataFrame(
            columns=[
                "排名",
                "股票代码",
                "股票名称",
                "观察类型",
                "阶段",
                "收盘价",
                "观察得分",
                "总分",
                "RS排名%",
                "量能比",
                "距突破位%",
                "距30周线%",
                "上方空间%",
                "基底区间%",
                "基底波动%",
                "10/30周差%",
                "突破位",
            ]
        )

    display = top_n.copy()
    display["阶段"] = display.apply(lambda row: get_trend_stage_label(row, config), axis=1)
    display["观察类型"] = display["watch_reason"]
    display["距突破位%"] = display["breakout_pct"]
    display["RS排名%"] = display["rs_rank_pct"]
    display["量能比"] = display["volume_ratio"]
    display["距30周线%"] = display["price_vs_ma_pct"]
    display["上方空间%"] = display["headroom_pct"]
    display["基底区间%"] = display["base_range_pct"]
    display["基底波动%"] = display["base_close_std_pct"]
    display["10/30周差%"] = display["ma_spread_pct"]
    display["观察得分"] = display["watch_score"]
    display["总分"] = display["total_score"]

    output = display[
        [
            "top_n_rank",
            "symbol",
            "name",
            "观察类型",
            "阶段",
            "close",
            "观察得分",
            "总分",
            "RS排名%",
            "量能比",
            "距突破位%",
            "距30周线%",
            "上方空间%",
            "基底区间%",
            "基底波动%",
            "10/30周差%",
            "breakout_level",
        ]
    ].rename(
        columns={
            "top_n_rank": "排名",
            "symbol": "股票代码",
            "name": "股票名称",
            "close": "收盘价",
            "breakout_level": "突破位",
        }
    )
    return output


def _format_stage2_top_n_for_reading(stage2_top_n: pd.DataFrame, config: AppConfig) -> pd.DataFrame:
    if stage2_top_n.empty:
        return pd.DataFrame(
            columns=[
                "排名",
                "股票代码",
                "股票名称",
                "阶段",
                "综合分",
                "结构分",
                "时机分",
                "强度分",
                "风险分",
                "观察说明",
                "收盘价",
                "量能比",
                "RS排名%",
                "基底区间%",
                "基底波动%",
                "10/30周差%",
                "距30周线%",
                "突破位",
                "距突破位%",
            ]
        )

    display = stage2_top_n.copy()
    display["阶段"] = display.apply(lambda row: get_trend_stage_label(row, config), axis=1)
    display["综合分"] = display["final_score"]
    display["结构分"] = display["structure_score"]
    display["时机分"] = display["timing_score"]
    display["强度分"] = display["strength_score"]
    display["风险分"] = display["risk_score"]
    display["观察说明"] = display["stage2_watch_reason"]
    display["量能比"] = display["volume_ratio"]
    display["RS排名%"] = display["rs_rank_pct"]
    display["基底区间%"] = display["base_range_pct"]
    display["基底波动%"] = display["base_close_std_pct"]
    display["10/30周差%"] = display["ma_spread_pct"]
    display["距30周线%"] = display["price_vs_ma_pct"]
    display["距突破位%"] = display["breakout_pct"]

    output = display[
        [
            "stage2_top_n_rank",
            "symbol",
            "name",
            "阶段",
            "综合分",
            "结构分",
            "时机分",
            "强度分",
            "风险分",
            "观察说明",
            "close",
            "量能比",
            "RS排名%",
            "基底区间%",
            "基底波动%",
            "10/30周差%",
            "距30周线%",
            "breakout_level",
            "距突破位%",
        ]
    ].rename(
        columns={
            "stage2_top_n_rank": "排名",
            "symbol": "股票代码",
            "name": "股票名称",
            "close": "收盘价",
            "breakout_level": "突破位",
        }
    )
    return output


def _safe_write_csv(frame: pd.DataFrame, primary_path: Path, fallback_path: Path) -> None:
    try:
        frame.to_csv(primary_path, index=False, encoding="utf-8-sig")
    except PermissionError:
        frame.to_csv(fallback_path, index=False, encoding="utf-8-sig")
