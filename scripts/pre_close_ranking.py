#!/usr/bin/env python3
"""
收盘前实时排行榜 (14:40 运行)
用腾讯实时行情替换当日日K数据，重新计算所有排行榜：
  - Stage I / Stage II / 准Stage2
  - 智能推荐 (W底)
  - 尾盘关注信号
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from winstan.config import AppConfig, load_config
from winstan.storage.duckdb_store import DuckDBStore
from winstan.rules.stage_analysis import apply_stage2_scoring
from winstan.scoring.ranker import score_and_rank, build_stage2_top_n, build_quasi_stage2_top_n
from winstan.patterns import compute_recommendations

# ── 常量 ──
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "strategy.yaml"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "reports" / "pre_close"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 腾讯实时行情字段索引
F_NAME = 1
F_CODE = 2
F_PRICE = 3
F_PRE_CLOSE = 4
F_OPEN = 5
F_VOLUME = 6
F_HIGH = 33
F_LOW = 34
F_CHANGE_PCT = 32
F_AMOUNT = 37
F_TIME = 30


def _safe_float(val) -> float:
    try:
        return float(val) if val else 0.0
    except (ValueError, TypeError):
        return 0.0


def _fetch_all_realtime() -> dict[str, dict]:
    """拉全A实时行情，返回 {symbol -> {price, volume, high, low, change_pct}}"""
    import requests

    # 构建所有A股代码 (sh/sz 主板+创业板+科创板)
    code_ranges = [
        # 上交所主板
        *(f"sh{str(i).zfill(6)}" for i in range(600000, 606000)),
        *(f"sh{str(i).zfill(6)}" for i in range(603000, 605000)),
        *(f"sh{str(i).zfill(6)}" for i in range(605000, 606000)),
        *(f"sh{str(i).zfill(6)}" for i in range(688000, 689000)),
        # 深交所 (000-001-002-003-300-301)
        *(f"sz{str(i).zfill(6)}" for i in range(0, 1000)),
        *(f"sz{str(i).zfill(6)}" for i in range(1000, 2000)),
        *(f"sz{str(i).zfill(6)}" for i in range(2000, 3000)),
        *(f"sz{str(i).zfill(6)}" for i in range(3000, 3100)),
        *(f"sz{str(i).zfill(6)}" for i in range(300000, 302000)),  # 300xxx + 301xxx
    ]

    result: dict[str, dict] = {}
    batch_size = 80
    session = requests.Session()

    for i in range(0, len(code_ranges), batch_size):
        batch = code_ranges[i : i + batch_size]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            resp = session.get(url, timeout=15)
            resp.encoding = "gbk"
            for line in resp.text.strip().split("\n"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                try:
                    raw = line.split("=", 1)[1].strip('"')
                    parts = raw.split("~")
                    if len(parts) < 40:
                        continue
                    code = parts[F_CODE]
                    price = _safe_float(parts[F_PRICE])
                    if price <= 0:
                        continue
                    # 转为统一 symbol 格式: 600519.SH
                    if code.startswith("6"):
                        symbol = f"{code}.SH"
                    elif code.startswith(("0", "3")):
                        symbol = f"{code}.SZ"
                    else:
                        continue
                    result[symbol] = {
                        "price": price,
                        "volume": int(float(parts[F_VOLUME]) if parts[F_VOLUME] else 0),
                        "high": _safe_float(parts[F_HIGH]),
                        "low": _safe_float(parts[F_LOW]),
                        "open": _safe_float(parts[F_OPEN]),
                        "pre_close": _safe_float(parts[F_PRE_CLOSE]),
                        "change_pct": _safe_float(parts[F_CHANGE_PCT]),
                        "amount": _safe_float(parts[F_AMOUNT]),
                        "name": parts[F_NAME],
                    }
                except (ValueError, IndexError):
                    continue
        except Exception as e:
            print(f"[preclose] Batch fetch error: {e}", file=sys.stderr)

    return result


def update_with_realtime(
    df: pd.DataFrame,
    realtime: dict[str, dict],
    config: AppConfig,
) -> pd.DataFrame:
    """
    用实时行情更新 screening_results：
      1. close ← 实时价
      2. volume ← 实时量
      3. price_vs_ma_pct ← 重新计算
      4. volume_ratio ← 估算
      5. breakout_pct, breakout_status ← 更新
    """
    updated = df.copy()
    mask = updated["symbol"].isin(realtime)
    updated.loc[~mask, "final_score"] = 0.0  # 无实时数据的置0

    for idx, row in updated[mask].iterrows():
        symbol = row["symbol"]
        rt = realtime.get(symbol)
        if not rt:
            continue

        price = rt["price"]
        volume = rt["volume"]
        pre_close = rt["pre_close"]
        ma_30w = float(row.get("ma_30w", 0) or 0)
        breakout_level = float(row.get("breakout_level", 0) or 0)

        # 更新价格
        updated.at[idx, "close"] = price
        updated.at[idx, "volume"] = volume

        # price_vs_ma_pct
        if ma_30w > 0:
            pct = (price / ma_30w - 1) * 100
            updated.at[idx, "price_vs_ma_pct"] = pct

        # 粗略估算 volume_ratio (今日量/昨量, 近似参考)
        updated.at[idx, "volume_ratio"] = volume / max(rt.get("pre_close_volume", 1), 1) * 0.01

        # breakout_pct & status
        if breakout_level > 0:
            bp = (price / breakout_level - 1) * 100
            updated.at[idx, "breakout_pct"] = bp
            # 如果实时价突破了之前的级别
            old_status = str(row.get("breakout_status", "below_breakout"))
            old_breakout_ok = bool(row.get("breakout_ok", False))

            if -2 <= bp <= 5:
                new_status = "just_broke_out"
                new_ok = True
            elif -5 <= bp < -2:
                new_status = "near_breakout"
                new_ok = False
            elif bp < -5:
                new_status = "below_breakout"
                new_ok = False
            else:
                new_status = "extended_breakout"
                new_ok = True

            updated.at[idx, "breakout_status"] = new_status
            updated.at[idx, "breakout_ok"] = new_ok

            if new_status == "just_broke_out" and old_status != "just_broke_out":
                updated.at[idx, "breakout_reason"] = "日内突破颈线"

    return updated


def format_ranking_section(title: str, items: list[dict], columns: list[str]) -> str:
    """格式化为 markdown 表格"""
    if not items:
        return f"**{title}**: 无数据\n"
    lines = [f"**{title}** ({len(items)}只)", ""]
    header = "| " + " | ".join(columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    lines.append(header)
    lines.append(sep)
    for item in items[:12]:
        row_data = []
        for col in columns:
            val = item.get(col, "")
            row_data.append(str(val))
        lines.append("| " + " | ".join(row_data) + " |")
    if len(items) > 12:
        lines.append(f"| ... 还有 {len(items)-12} 只 |")
    lines.append("")
    return "\n".join(lines)


def format_rec_section(items: list[dict]) -> str:
    """格式化智能推荐"""
    lines = []
    for level, label in [("S", "S级 强烈推荐"), ("A", "A级 建议买入"), ("B+", "B+级 提前埋伏"), ("B", "B级 关注")]:
        level_items = [i for i in items if i["rec_level"] == level]
        if level_items:
            lines.append(f"**{label}** ({len(level_items)}只)")
            lines.append("| 代码 | 名称 | 评分 | 理由 | 距颈线 |")
            lines.append("|------|------|------|------|--------|")
            for i in level_items[:8]:
                wb = i.get("w_bottom") or {}
                neck = wb.get("neckline", "--")
                pct = i.get("close", "--")
                lines.append(
                    f"| {i['symbol']} | {i['name']} | {i['rec_score']} | {i['rec_reason'][:30]} | {pct}/{neck} |"
                )
            if len(level_items) > 8:
                lines.append(f"| ... 还有 {len(level_items)-8} 只 |")
            lines.append("")
    return "\n".join(lines)


def format_push_message(
    stage2: list[dict],
    quasi: list[dict],
    stage1: list[dict],
    recs: list[dict],
    rt_alerts: list[dict],
    timestamp: str,
) -> str:
    """生成推送消息"""
    lines = [
        "📊 **收盘前实时排行榜** (14:40)",
        f"⏱ {timestamp}",
        "",
    ]

    # 尾盘关注（实时信号）
    urgent = [a for a in rt_alerts if a.get("level") == "🔥 紧急"]
    normal = [a for a in rt_alerts if a.get("level") != "🔥 紧急"]
    if urgent:
        lines.append("🔥 **实时关注**")
        lines.append("| 代码 | 名称 | 现价 | 涨幅 | 距颈线 | 理由 |")
        lines.append("|------|------|------|------|--------|------|")
        for a in urgent[:6]:
            lines.append(f"| {a['symbol']} | {a['name']} | {a['price']} | {a['change_pct']} | {a['pct_to_neck']} | {a['reasons']} |")
        lines.append("")
    if normal:
        lines.append("⭐ **关注**")
        lines.append("| 代码 | 名称 | 现价 | 距颈线 | 理由 |")
        lines.append("|------|------|------|--------|------|")
        for a in normal[:6]:
            lines.append(f"| {a['symbol']} | {a['name']} | {a['price']} | {a['pct_to_neck']} | {a['reasons']} |")
        lines.append("")

    # 准Stage2
    quasi_cols = ["rank", "symbol", "name", "total_score", "breakout_status"]
    q_list = []
    for i, item in enumerate(quasi):
        q_list.append({
            "rank": str(item.get("rank", i + 1)),
            "symbol": str(item.get("symbol", "")),
            "name": str(item.get("name", "")),
            "total_score": f'{item.get("total_score", 0):.1f}',
            "breakout_status": str(item.get("breakout_status", "")),
        })
    lines.append(format_ranking_section("准Stage2", q_list, ["rank", "symbol", "name", "total_score", "breakout_status"]))

    # smart recs
    lines.append(format_rec_section(recs))

    # Stage1
    s1_list = []
    for item in stage1[:8]:
        s1_list.append({
            "rank": str(item.get("rank", "")),
            "symbol": str(item.get("symbol", "")),
            "name": str(item.get("name", "")),
            "total_score": f'{item.get("total_score", 0):.1f}',
        })
    lines.append(format_ranking_section("Stage I", s1_list, ["rank", "symbol", "name", "total_score"]))

    return "\n".join(lines)


def compute_intraday_alerts(
    recs: list[dict],
    realtime: dict[str, dict],
) -> list[dict]:
    """基于实时行情计算尾盘信号"""
    alerts = []
    for rec in recs:
        symbol = rec["symbol"]
        rt = realtime.get(symbol)
        if not rt:
            continue

        wb = rec.get("w_bottom") or {}
        neckline = float(wb.get("neckline", 0)) if isinstance(wb, dict) else 0
        pattern_score = float(wb.get("pattern_score", 0)) if isinstance(wb, dict) else 0
        if not neckline or not pattern_score:
            continue

        price = rt["price"]
        change_pct = rt["change_pct"]
        pct_to_neck = (price / neckline - 1) * 100

        if abs(pct_to_neck) > 15:
            continue

        urgency = 0
        reasons = []

        if pct_to_neck < 0:
            urgency += 4
            reasons.append(f"距颈线{abs(pct_to_neck):.1f}%待突破")
        elif pct_to_neck < 5:
            urgency += 3
            reasons.append(f"刚突破颈线{pct_to_neck:.1f}%")
        elif pct_to_neck < 10:
            urgency += 2
            reasons.append(f"突破颈线{pct_to_neck:.1f}%")
        else:
            urgency += 1
            reasons.append(f"已突破{pct_to_neck:.1f}%")

        if 1 <= change_pct <= 4:
            urgency += 2
            reasons.append(f"稳步{change_pct:+.1f}%")
        elif change_pct > 4 and pct_to_neck < 10:
            urgency += 1
            reasons.append(f"加速{change_pct:+.1f}%")

        if pattern_score >= 80:
            urgency += 1

        if urgency < 3:
            continue

        alerts.append({
            "symbol": symbol,
            "name": rt.get("name", ""),
            "price": f"{price:.2f}",
            "change_pct": f"{change_pct:+.2f}%",
            "pct_to_neck": f"{pct_to_neck:+.1f}%",
            "reasons": " | ".join(reasons),
            "level": "🔥 紧急" if urgency >= 6 else "⭐ 关注",
            "urgency": urgency,
        })

    alerts.sort(key=lambda a: -a["urgency"])
    return alerts


def main() -> None:
    t_start = datetime.now()
    config = load_config(CONFIG_PATH)
    store = DuckDBStore(config.duckdb_path)

    # 1. 拉全A实时行情
    print("[preclose] Fetching all A-share real-time data...")
    realtime = _fetch_all_realtime()
    print(f"[preclose] Got {len(realtime)} real-time quotes")

    # 2. 存一份 intraday 快照到 parquet（Dashboard 直接可读）
    print("[preclose] Saving intraday snapshot to parquet...")
    today_str = datetime.now().strftime("%Y-%m-%d")
    intra_rows = []
    for sym, rt in realtime.items():
        intra_rows.append({
            "symbol": sym,
            "trade_date": today_str,
            "open": rt.get("open", 0),
            "high": rt.get("high", 0),
            "low": rt.get("low", 0),
            "close": rt["price"],
            "volume": rt.get("volume", 0),
            "amount": rt.get("amount", 0),
            "source": "tencent",
        })
    intraday_df = pd.DataFrame(intra_rows)
    config = load_config(CONFIG_PATH)
    from winstan.storage.parquet_store import ParquetStore
    pstore = ParquetStore(config.parquet_root)
    pstore.write_intraday_snapshot(today_str, intraday_df)
    print(f"[preclose] Saved intraday snapshot: {len(intraday_df)} stocks")

    # 3. 读取现有 screening_results
    print("[preclose] Reading screening results...")
    with store.connect() as conn:
        results = conn.execute("SELECT * FROM screening_results").fetchdf()
    print(f"[preclose] {len(results)} rows loaded")

    # 3. 用实时数据更新
    print("[preclose] Updating with real-time data...")
    updated = update_with_realtime(results, realtime, config)
    updated = apply_stage2_scoring(updated, config)

    # 4. 重新跑排行榜
    print("[preclose] Computing rankings...")
    _, stage1 = score_and_rank(updated, config)
    stage2 = build_stage2_top_n(updated, config)
    quasi = build_quasi_stage2_top_n(updated, config)

    # 5. 重新跑智能推荐
    print("[preclose] Computing recommendations...")
    recs = compute_recommendations(updated, config.parquet_root)

    # 演示输出
    print(f"\nStage I: {len(stage1)} | Stage II: {len(stage2)} | 准Stage2: {len(quasi)} | 推荐: {len(recs)}")

    # 6. 尾盘信号
    alerts = compute_intraday_alerts(recs, realtime)
    print(f"实时信号: {len(alerts)} 只 ({len([a for a in alerts if a['level']=='🔥 紧急'])}紧急)")

    # 7. 序列化为可推送给前端展示
    def _serialize(df, rank_col="rank"):
        if df.empty:
            return []
        # 只保留关键列，转 JSON 兼容类型
        keep_cols = ["symbol", "name", "stage_label", "final_score", "breakout_status",
                     "price_vs_ma_pct", "volume_ratio"]
        avail = [c for c in keep_cols if c in df.columns]
        items = df[avail].copy().to_dict(orient="records")
        for i, item in enumerate(items):
            item[rank_col] = i + 1
            # 清理非JSON类型
            for k, v in item.items():
                if isinstance(v, (pd.Timestamp, np.datetime64)):
                    item[k] = str(v)
                elif isinstance(v, (np.floating,)):
                    item[k] = float(v)
                elif isinstance(v, (np.integer,)):
                    item[k] = int(v)
        return items

    payload = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": (datetime.now() - t_start).total_seconds(),
        "realtime_count": len(realtime),
        "stage1": _serialize(stage1, "rank"),
        "stage2": _serialize(stage2, "rank"),
        "quasi_stage2": _serialize(quasi, "rank"),
        "recommendations": recs,
        "alerts": alerts,
    }

    # 保存
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = OUTPUT_DIR / f"pre_close_ranking_{ts}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"[preclose] Saved to {out_path}")

    # 推送消息
    msg = format_push_message(
        stage2=_serialize(stage2),
        quasi=_serialize(quasi),
        stage1=_serialize(stage1),
        recs=recs,
        rt_alerts=alerts,
        timestamp=payload["timestamp"],
    )
    print("\n" + "=" * 50)
    print(msg)
    print("=" * 50)

    print("\n---PUSH_MESSAGE_START---")
    print(msg)
    print("---PUSH_MESSAGE_END---")


if __name__ == "__main__":
    main()
