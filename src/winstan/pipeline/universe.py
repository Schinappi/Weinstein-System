from __future__ import annotations

from datetime import timedelta

import pandas as pd

from winstan.config import AppConfig


def build_universe(raw_universe: pd.DataFrame, config: AppConfig) -> pd.DataFrame:
    frame = raw_universe.copy()
    if config.universe.mode == "custom_list":
        custom = set(config.universe.custom_symbols)
        frame = frame[frame["symbol"].isin(custom)]

    frame = _exclude_symbol_prefixes(frame, config.universe.excluded_symbol_prefixes)
    if config.universe.exclude_st and "is_st" in frame.columns:
        frame = frame[~frame["is_st"].fillna(False)]

    if "list_date" in frame.columns and config.universe.exclude_new_listing_days:
        cutoff = pd.Timestamp.today().normalize() - timedelta(days=config.universe.exclude_new_listing_days)
        frame = frame[frame["list_date"].isna() | (frame["list_date"] <= cutoff)]

    return frame.drop_duplicates(subset=["symbol"]).reset_index(drop=True)


def _exclude_symbol_prefixes(frame: pd.DataFrame, prefixes: list[str]) -> pd.DataFrame:
    if frame.empty or "symbol" not in frame.columns or not prefixes:
        return frame

    normalized_prefixes = tuple(str(prefix).strip() for prefix in prefixes if str(prefix).strip())
    if not normalized_prefixes:
        return frame

    symbol_head = (
        frame["symbol"]
        .fillna("")
        .astype(str)
        .str.upper()
        .str.replace(r"^(SH|SZ|BJ)", "", regex=True)
        .str.split(".", n=1)
        .str[0]
    )
    return frame[~symbol_head.str.startswith(normalized_prefixes)]
