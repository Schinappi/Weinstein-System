"""Tushare API client initialization.

Primary: ``tushare`` library → ``http://a.sszhixia.cn/`` (custom API endpoint).
Fallback: ``chinadata.ca_data`` → chinadata default server.

Usage::

    from winstan.adapters.tushare_client import build_tushare_pro, build_chinadata_pro

    ts_mod, pro = build_tushare_pro()
    df = pro.daily(ts_code="000001.SZ", start_date="20260601", end_date="20260605")

    # Fallback:
    ts_fb, pro_fb = build_chinadata_pro()
"""

from __future__ import annotations

import os

# ── Primary API config (tushare → a.sszhixia.cn) ──
DEFAULT_TOKEN: str | None = None

# ── Fallback API config (chinadata) ──
DEFAULT_CHINADATA_TOKEN = "a578bfb4d131b134844e4fbc4a68960dd91"


def _disable_proxy_env() -> None:
    """Remove proxy environment variables that interfere with local API calls."""
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


def build_tushare_pro(token: str | None = None, api_url: str | None = None):
    """Create a Tushare pro client.

    Parameters
    ----------
    token:
        Falls back to ``TUSHARE_TOKEN`` env var.
    api_url:
        Optional override for the underlying Tushare API URL.

    Returns
    -------
    tuple[module, pro_api]
        ``(tushare_module, pro_client)``.
    """
    _disable_proxy_env()

    import tushare as ts

    resolved_token = token or os.getenv("TUSHARE_TOKEN") or DEFAULT_TOKEN
    if not resolved_token:
        raise ValueError("TUSHARE_TOKEN is required.")
    resolved_url = api_url or os.getenv("TUSHARE_API_URL") or None

    pro = ts.pro_api(resolved_token)
    if resolved_url:
        pro._DataApi__http_url = resolved_url

    try:
        pro._DataApi__timeout = 20
    except Exception:
        pass

    return ts, pro


def build_chinadata_pro(token: str | None = None):
    """Create a chinadata pro client (fallback).

    Parameters
    ----------
    token:
        Falls back to ``CHINADATA_TOKEN`` env var, then ``DEFAULT_CHINADATA_TOKEN``.

    Returns
    -------
    tuple[module, pro_api]
        ``(chinadata_module, pro_client)``.
    """
    _disable_proxy_env()

    import chinadata.ca_data as ts

    resolved_token = token or os.getenv("CHINADATA_TOKEN") or DEFAULT_CHINADATA_TOKEN
    if not resolved_token:
        raise ValueError("CHINADATA_TOKEN is required.")

    pro = ts.pro_api(resolved_token)
    try:
        pro._DataApi__timeout = 20
    except Exception:
        pass

    return ts, pro


def smoke_test() -> tuple[object, object]:
    """Quick connectivity test: index_basic + pro_bar for 000001.SZ."""
    ts, pro = build_tushare_pro()
    index_basic = pro.index_basic(limit=5)
    daily_bars = ts.pro_bar(api=pro, ts_code="000001.SZ", limit=3)
    return index_basic, daily_bars
