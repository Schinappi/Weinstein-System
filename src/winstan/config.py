from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


def normalize_date_like(value: str | date | datetime | None, fallback: str | None = None) -> str | None:
    if value is None:
        return fallback
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists() or not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
            value = value[1:-1]
        os.environ[key] = value


@dataclass(slots=True)
class UniverseConfig:
    mode: str = "all_a"
    custom_symbols: list[str] = field(default_factory=list)
    exclude_st: bool = True
    exclude_new_listing_days: int = 180
    min_avg_turnover: float | None = None


@dataclass(slots=True)
class DataConfig:
    primary_source: str = "tushare"
    fallback_source: str = "tickflow"
    adjust_type: str = "forward"
    batch_size: int = 100
    tushare_calls_per_minute: int = 400
    tushare_retry_times: int = 3
    tushare_retry_sleep_seconds: float = 5.0
    tushare_timeout_seconds: int = 20
    start_date: str | date | datetime = "2018-01-01"
    end_date: str | date | datetime | None = None
    use_cache: bool = True
    force_refresh: bool = False
    tushare_token_env: str = "TUSHARE_TOKEN"
    tickflow_api_key_env: str = "TICKFLOW_API_KEY"
    tickflow_base_url: str = "https://api.tickflow.org"
    tickflow_free_base_url: str = "https://free-api.tickflow.org"

    @property
    def effective_start_date(self) -> str:
        return normalize_date_like(self.start_date, "2018-01-01") or "2018-01-01"

    @property
    def effective_end_date(self) -> str:
        return normalize_date_like(self.end_date, date.today().isoformat()) or date.today().isoformat()

    @property
    def tushare_token(self) -> str | None:
        env_value = os.getenv(self.tushare_token_env)
        if env_value:
            return env_value
        if isinstance(self.tushare_token_env, str) and len(self.tushare_token_env) >= 32 and self.tushare_token_env != "TUSHARE_TOKEN":
            return self.tushare_token_env
        return None

    @property
    def tickflow_api_key(self) -> str | None:
        env_value = os.getenv(self.tickflow_api_key_env)
        if env_value:
            return env_value
        if self.tickflow_api_key_env.startswith("tk_"):
            return self.tickflow_api_key_env
        return None


@dataclass(slots=True)
class MarketConfig:
    benchmark_symbol: str = "000906.SH"
    use_market_filter: bool = True


@dataclass(slots=True)
class StrategyConfig:
    ma_window_weeks: int = 30
    short_ma_window_weeks: int = 10
    ma_slope_lookback_weeks: int = 4
    volume_avg_weeks: int = 10
    daily_volume_avg_days: int = 50
    rs_rank_threshold_pct: int = 30
    resistance_min_headroom_pct: float = 12.0
    resistance_lookback_weeks: int = 52
    breakout_lookback_weeks: int = 10
    breakout_min_pct: float = 1.5
    enable_breakout_filter: bool = False
    min_stage2_weeks: int = 5
    watch_near_breakout_pct: float = 3.0
    watch_breakout_max_pct: float = 8.0
    watch_max_price_vs_ma_pct: float = 15.0
    watch_base_lookback_weeks: int = 12
    watch_base_max_range_pct: float = 25.0
    watch_base_max_close_std_pct: float = 6.0
    watch_ma_spread_max_pct: float = 5.0


@dataclass(slots=True)
class RankingConfig:
    top_n: int = 20
    stage2_top_n: int = 10
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "trend": 0.25,
            "rs": 0.30,
            "volume": 0.25,
            "resistance": 0.15,
            "breakout_bonus": 0.05,
        }
    )


@dataclass(slots=True)
class OutputConfig:
    export_candidates: bool = True
    export_top_n: bool = True
    export_summary: bool = True
    export_debug: bool = False
    reports_dir: str = "reports"
    logs_dir: str = "logs"


@dataclass(slots=True)
class LLMConfig:
    enabled: bool = True
    provider: str = "deepseek"
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    timeout_seconds: int = 45
    temperature: float = 0.35
    max_tokens: int = 900

    @property
    def api_key(self) -> str | None:
        env_value = os.getenv(self.api_key_env)
        if env_value:
            return env_value
        if self.api_key_env.startswith("sk-"):
            return self.api_key_env
        return None


@dataclass(slots=True)
class AppConfig:
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    data: DataConfig = field(default_factory=DataConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    ranking: RankingConfig = field(default_factory=RankingConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    project_root: Path = field(default_factory=lambda: Path.cwd())

    @property
    def data_root(self) -> Path:
        return self.project_root / "data"

    @property
    def parquet_root(self) -> Path:
        return self.data_root / "parquet"

    @property
    def duckdb_path(self) -> Path:
        return self.data_root / "duckdb" / "market_data.duckdb"

    @property
    def reports_dir(self) -> Path:
        return self.project_root / self.output.reports_dir

    @property
    def logs_dir(self) -> Path:
        return self.project_root / self.output.logs_dir


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str | Path) -> AppConfig:
    config_path = Path(config_path)
    project_root = config_path.resolve().parent.parent
    _load_env_file(project_root / ".env")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    defaults = AppConfig(project_root=project_root)
    merged = _merge_dict(
        {
            "universe": asdict(defaults.universe),
            "data": asdict(defaults.data),
            "market": asdict(defaults.market),
            "strategy": asdict(defaults.strategy),
            "ranking": asdict(defaults.ranking),
            "output": asdict(defaults.output),
            "llm": asdict(defaults.llm),
        },
        raw,
    )

    return AppConfig(
        universe=UniverseConfig(**merged["universe"]),
        data=DataConfig(**merged["data"]),
        market=MarketConfig(**merged["market"]),
        strategy=StrategyConfig(**merged["strategy"]),
        ranking=RankingConfig(**merged["ranking"]),
        output=OutputConfig(**merged["output"]),
        llm=LLMConfig(**merged["llm"]),
        project_root=project_root,
    )
