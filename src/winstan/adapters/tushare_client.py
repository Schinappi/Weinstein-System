from __future__ import annotations

import os


def _disable_proxy_env() -> None:
    for key in [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ]:
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def build_tushare_pro(token: str | None = None):
    """Create a Tushare pro client with the required custom endpoint."""
    _disable_proxy_env()
    try:
        import chinadata.ca_data as ts
    except ImportError as exc:
        raise ImportError("chinadata is not installed. Install with `pip install chinadata`.") from exc

    resolved_token = token or os.getenv("TUSHARE_TOKEN")
    if not resolved_token:
        raise ValueError("TUSHARE_TOKEN is required. Set it in .env or the environment.")
    pro = ts.pro_api(resolved_token)
    try:
        pro._DataApi__timeout = 20
    except Exception:
        pass
    return ts, pro


def smoke_test() -> tuple[object, object]:
    """Run the user-provided initialization example."""
    ts, pro = build_tushare_pro()
    index_basic = pro.index_basic(limit=5)
    daily_bars = pro.daily(ts_code="000001.SZ", start_date="20180701", end_date="20180718")
    return index_basic, daily_bars
