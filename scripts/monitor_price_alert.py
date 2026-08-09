#!/usr/bin/env python3
"""Poll monitored stocks and send PushPlus alerts near their target prices."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from winstan.config import load_config
from winstan.storage.price_monitor_store import PriceMonitorStore


# Adjust this value to change the normal polling interval.
CHECK_INTERVAL_SECONDS = 3 * 60
TARGET_DISTANCE_THRESHOLD_PCT = 2.0
REQUEST_TIMEOUT_SECONDS = 15
PUSHPLUS_TOKEN_ENV = "PUSHPLUS_TOKEN"
PUSHPLUS_ENDPOINT = "https://www.pushplus.plus/send"
TENCENT_QUOTE_ENDPOINT = "https://qt.gtimg.cn/q={codes}"
CONFIG_PATH = PROJECT_ROOT / "config" / "strategy.yaml"
STATE_PATH = PROJECT_ROOT / "logs" / "monitor_price_alert_state.json"
MAX_TENCENT_BATCH_SIZE = 80

LOGGER = logging.getLogger("monitor_price_alert")


def _safe_float(value: object) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        parsed = float(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _normalize_symbol(symbol: object) -> str:
    text = str(symbol or "").strip().upper()
    if "." not in text:
        if text.startswith("6"):
            return f"{text}.SH"
        if text.startswith(("0", "3")):
            return f"{text}.SZ"
        if text.startswith(("8", "4", "9")):
            return f"{text}.BJ"
        return text
    code, exchange = text.split(".", 1)
    return f"{code}.{exchange[:2]}"


def _to_tencent_symbol(symbol: object) -> str:
    normalized = _normalize_symbol(symbol)
    code, exchange = normalized.split(".", 1)
    return f"{exchange.lower()}{code}"


def _fetch_tencent_quotes(symbols: list[str]) -> dict[str, dict[str, object]]:
    quotes: dict[str, dict[str, object]] = {}
    request_symbols = [_to_tencent_symbol(symbol) for symbol in symbols]
    for start in range(0, len(request_symbols), MAX_TENCENT_BATCH_SIZE):
        batch = request_symbols[start : start + MAX_TENCENT_BATCH_SIZE]
        response = requests.get(
            TENCENT_QUOTE_ENDPOINT.format(codes=",".join(batch)),
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.encoding = "gbk"
        response.raise_for_status()
        for raw_line in response.text.splitlines():
            line = raw_line.strip()
            if "=" not in line:
                continue
            variable, raw_value = line.split("=", 1)
            parts = raw_value.strip().strip(";").strip('"').split("~")
            if len(parts) < 6:
                continue
            market_code = variable.rsplit("_", 1)[-1]
            market = market_code[:2].upper()
            code = parts[2].strip()
            symbol = _normalize_symbol(f"{code}.{market}")
            price = _safe_float(parts[3])
            if price is None:
                continue
            quotes[symbol] = {
                "symbol": symbol,
                "name": parts[1].strip(),
                "price": price,
                "quote_time": parts[30].strip() if len(parts) > 30 else "",
            }
    return quotes


def _load_alert_state() -> dict[str, dict[str, object]]:
    if not STATE_PATH.exists():
        return {}
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Unable to read alert state; starting with an empty state.")
        return {}


def _save_alert_state(state: dict[str, dict[str, object]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _send_pushplus_alert(alerts: list[dict[str, object]]) -> bool:
    token = os.getenv(PUSHPLUS_TOKEN_ENV, "").strip()
    if not token:
        LOGGER.error("Missing %s; add it to .env or the process environment.", PUSHPLUS_TOKEN_ENV)
        return False

    content_lines = ["以下监控股票当前价格已进入目标价 ±2% 范围：", ""]
    for alert in alerts:
        content_lines.append(
            f"{alert['symbol']} {alert['name']} | "
            f"现价 {alert['price']:.2f} | 目标价 {alert['target_price']:.2f} | "
            f"距离 {alert['distance_pct']:+.2f}%"
        )
    payload = {
        "token": token,
        "title": "价格监控命中",
        "content": "\n".join(content_lines),
        "template": "txt",
    }
    try:
        response = requests.post(
            PUSHPLUS_ENDPOINT,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        result = response.json()
        if str(result.get("code")) != "200":
            LOGGER.error("PushPlus rejected alert: %s", result)
            return False
        LOGGER.info("PushPlus alert sent for %d stock(s).", len(alerts))
        return True
    except (requests.RequestException, ValueError) as exc:
        LOGGER.error("PushPlus request failed: %s", exc)
        return False


def check_once(store: PriceMonitorStore) -> int:
    monitors = store.list_items()
    if monitors.empty:
        LOGGER.info("No price monitors configured.")
        _save_alert_state({})
        return 0

    symbols = [_normalize_symbol(value) for value in monitors["symbol"].tolist()]
    quotes = _fetch_tencent_quotes(symbols)
    previous_state = _load_alert_state()
    next_state: dict[str, dict[str, object]] = {}
    new_alerts: list[dict[str, object]] = []

    for _, row in monitors.iterrows():
        monitor_id = str(row.get("id") or "")
        symbol = _normalize_symbol(row.get("symbol"))
        target_price = _safe_float(row.get("target_price"))
        quote = quotes.get(symbol)
        if not monitor_id or target_price is None:
            continue
        if quote is None:
            LOGGER.warning("Tencent quote missing for %s.", symbol)
            if monitor_id in previous_state:
                next_state[monitor_id] = previous_state[monitor_id]
            continue

        price = float(quote["price"])
        distance_pct = (price - target_price) / target_price * 100.0
        inside_threshold = abs(distance_pct) <= TARGET_DISTANCE_THRESHOLD_PCT
        old = previous_state.get(monitor_id, {})
        old_target = _safe_float(old.get("target_price"))
        was_inside = bool(old.get("inside")) and old_target == target_price
        next_state[monitor_id] = {
            "symbol": symbol,
            "target_price": target_price,
            "inside": inside_threshold if not inside_threshold else was_inside,
            "last_price": price,
        }
        if inside_threshold and not was_inside:
            new_alerts.append({
                "monitor_id": monitor_id,
                "symbol": symbol,
                "name": str(row.get("name") or quote.get("name") or ""),
                "price": price,
                "target_price": target_price,
                "distance_pct": distance_pct,
            })

    if new_alerts and _send_pushplus_alert(new_alerts):
        for alert in new_alerts:
            monitor_id = str(alert["monitor_id"])
            if monitor_id in next_state:
                next_state[monitor_id]["inside"] = True
    _save_alert_state(next_state)
    LOGGER.info(
        "Checked %d monitor(s), got %d quote(s), new hit(s)=%d.",
        len(monitors),
        len(quotes),
        len(new_alerts),
    )
    return len(new_alerts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll price monitors and send PushPlus alerts.")
    parser.add_argument("--once", action="store_true", help="Run one check and exit.")
    parser.add_argument(
        "--interval",
        type=int,
        default=CHECK_INTERVAL_SECONDS,
        help=f"Polling interval in seconds (default: {CHECK_INTERVAL_SECONDS}).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config(CONFIG_PATH)
    store = PriceMonitorStore(config.duckdb_path)
    interval = max(1, args.interval)

    while True:
        try:
            check_once(store)
        except Exception:
            LOGGER.exception("Price monitor check failed.")
        if args.once:
            return
        LOGGER.info("Next check in %d seconds.", interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
