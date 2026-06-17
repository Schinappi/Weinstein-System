import pandas as pd

def _compute_box_daily_boundaries(
    daily_sorted: pd.DataFrame,
    weekly: pd.DataFrame,
    a_h: float, b_h: float,
    a_l: float, b_l: float,
    box_start_week: int,
    box_end_week: int,
    seg_offset: int,
    frame_len: int,
) -> tuple:
    """Map weekly box regression to daily bar arrays for chart rendering.

    The regression was fitted on a 30-week segment (indices 0..29).
    seg_offset = len(weekly) - 30 (or len(weekly) - min(30, len(weekly))).

    Returns (box_upper, box_lower, box_start_idx, box_end_idx) where:
    - box_upper/box_lower are arrays of length frame_len (None where no box)
    - box_start_idx/box_end_idx are daily indices into frame
    """
    weekly_sorted = weekly.sort_values("trade_date").reset_index(drop=True)
    n_weeks = len(weekly_sorted)

    bs = max(0, min(box_start_week, n_weeks - 1))
    be = max(bs, min(box_end_week, n_weeks - 1))

    box_start_date = weekly_sorted.iloc[bs]["trade_date"]
    box_end_date = weekly_sorted.iloc[be]["trade_date"]

    daily_dates = daily_sorted["trade_date"].values[-frame_len:]
    daily_start_idx = None
    daily_end_idx = None

    for i, d in enumerate(daily_dates):
        if daily_start_idx is None and d >= box_start_date:
            daily_start_idx = i
        if d <= box_end_date:
            daily_end_idx = i

    if daily_start_idx is None or daily_end_idx is None:
        return None, None, None, None

    weekly_dates = weekly_sorted["trade_date"].values
    box_upper = [None] * frame_len
    box_lower = [None] * frame_len

    for i in range(daily_start_idx, daily_end_idx + 1):
        d = daily_dates[i]
        week_idx = None
        for w in range(len(weekly_dates)):
            if weekly_dates[w] >= d:
                week_idx = w
                break
        if week_idx is None:
            week_idx = len(weekly_dates) - 1

        # Convert to segment-relative index for regression coefficients
        seg_idx = week_idx - seg_offset
        upper = a_h * seg_idx + b_h
        lower = a_l * seg_idx + b_l
        if lower > 0 and upper > lower:
            box_upper[i] = round(float(upper), 2)
            box_lower[i] = round(float(lower), 2)

    return box_upper, box_lower, daily_start_idx, daily_end_idx
