from __future__ import annotations

from datetime import timedelta

import pandas as pd

from winstan.config import AppConfig


def build_universe(raw_universe: pd.DataFrame, config: AppConfig) -> pd.DataFrame:
    if config.universe.mode == "custom_list":
        custom = set(config.universe.custom_symbols)
        return raw_universe[raw_universe["symbol"].isin(custom)].reset_index(drop=True)

    frame = raw_universe.copy()
    if config.universe.exclude_st and "is_st" in frame.columns:
        frame = frame[~frame["is_st"].fillna(False)]

    if "list_date" in frame.columns and config.universe.exclude_new_listing_days:
        cutoff = pd.Timestamp.today().normalize() - timedelta(days=config.universe.exclude_new_listing_days)
        frame = frame[frame["list_date"].isna() | (frame["list_date"] <= cutoff)]

    return frame.drop_duplicates(subset=["symbol"]).reset_index(drop=True)

