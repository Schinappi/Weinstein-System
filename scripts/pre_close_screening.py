#!/usr/bin/env python3
"""
尾盘前实时选股 (14:50 运行)
使用 AKShare 获取实时行情，结合 W底形态 + 温斯坦评分，
筛选出当日尾盘值得买入的候选股。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from winstan.config import load_config
from winstan.storage.duckdb_store import DuckDBStore
from winstan.dashboard.service import DashboardService
from winstan.patterns import compute_recommendations

# ── 常量 ──
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "reports" / "pre_close"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "strategy.yaml"

# AKShare 代码前缀映射
EXCHANGE_MAP = {
    "sh": "SH",
    "sz": "SZ",
    "bj": "BJ",
}


def _safe_float(val) -> float:
    try:
        return float(val) if val else 0.0
    except (ValueError, TypeError):
        return 0.0


def normalize_code(ak_code: str) -> str:
    """将 AKShare 代码转为统一格式，如 'sh600519' -> '600519.SH'"""
    code = ak_code.strip()
    if "." in code:
        return code.upper()
    prefix = code[:2].lower()
    num = code[2:]
    if prefix in EXCHANGE_MAP:
        return f"{num}.{EXCHANGE_MAP[prefix]}"
    return code


def fetch_realtime(symbols: list[str] | None = None) -> pd.DataFrame:
    """通过腾讯行情接口获取实时数据（免费、稳定、盘中可用）"""
    import requests

    # 腾讯代码格式：sh600519, sz000333
    rows = []
    # 如果提供了symbols，只查这些；否则查候选池
    batch = symbols if symbols else []
    if not batch:
        return pd.DataFrame()

    # 每批最多查 80 只
    chunk_size = 80
    for i in range(0, len(batch), chunk_size):
        chunk = batch[i : i + chunk_size]
        codes = ",".join(chunk)
        url = f"https://qt.gtimg.cn/q={codes}"
        resp = requests.get(url, timeout=15)
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
                rows.append(
                    {
                        "symbol": parts[2],
                        "name": parts[1],
                        "price": _safe_float(parts[3]),
                        "pre_close": _safe_float(parts[4]),
                        "open": _safe_float(parts[5]),
                        "volume": int(float(parts[6]) if parts[6] else 0),
                        "high": _safe_float(parts[33]),
                        "low": _safe_float(parts[34]),
                        "change_pct": _safe_float(parts[32]),
                        "time": parts[30],
                        "amount": _safe_float(parts[37]),
                    }
                )
            except (ValueError, IndexError):
                continue

    df = pd.DataFrame(rows)
    if not df.empty:
        df["symbol"] = df["symbol"].apply(
            lambda c: f"{c}.SH" if c.startswith("6") else f"{c}.SZ" if c.startswith(("0", "3")) else c
        )
    return df


def get_candidates(service: DashboardService) -> pd.DataFrame:
    """从 DuckDB 获取准Stage2 + W底候选的股票列表"""
    results = service.get_results()
    # 和推荐模块使用相同的候选池
    candidates = results[results.get("stage2_candidate", False)].copy()
    stage1_watch = results[
        (results["stage_label"] == "I") & (results.get("base_flatness_ok", False))
    ].head(50).copy()
    target = pd.concat([candidates, stage1_watch], ignore_index=True)
    target = target.drop_duplicates(subset=["symbol"])
    return target


def compute_intraday_alert(
    candidates: pd.DataFrame,
    recommendations: list[dict],
    realtime: pd.DataFrame,
) -> dict[str, object]:
    """
    结合实时行情，计算尾盘关注信号。
    返回按紧迫度排序的列表。
    """
    # 建立实时行情索引
    rt_map = realtime.set_index("symbol")

    alerts: list[dict[str, object]] = []

    # 建立推荐索引 (symbol -> rec)
    rec_map = {}
    for r in recommendations:
        rec_map[r["symbol"]] = r

    for _, row in candidates.iterrows():
        symbol = str(row.get("symbol", ""))
        if not symbol or symbol not in rt_map.index:
            continue

        rt = rt_map.loc[symbol]
        price = float(rt.get("price", 0) or 0)
        change_pct = float(rt.get("change_pct", 0) or 0)
        volume = float(rt.get("volume", 0) or 0)
        pre_close = float(rt.get("pre_close", 0) or 0)

        rec = rec_map.get(symbol)
        wb_info = rec.get("w_bottom") if rec else None

        neckline = float(wb_info.get("neckline", 0)) if wb_info and isinstance(wb_info, dict) else 0
        pattern_score = float(wb_info.get("pattern_score", 0)) if wb_info and isinstance(wb_info, dict) else 0
        weinstein_score = float(rec.get("weinstein_score", "0").replace("--", "0") or 0) if rec else 0

        pct_to_neck = (price / neckline - 1) * 100 if neckline > 0 else None

        # 评分逻辑：聚焦颈线附近未暴涨的候选
        urgency = 0
        reasons = []

        # 距颈线太远（>15%）→ 已暴涨过，不追
        if pct_to_neck is not None and abs(pct_to_neck) > 15:
            continue

        # 1. 距离颈线位置（核心指标）
        if pct_to_neck is not None:
            if pct_to_neck < 0:
                # 还在颈线下方 → 突破前埋伏（最佳机会）
                urgency += 4
                reasons.append(f"距颈线{abs(pct_to_neck):.1f}%待突破")
            elif pct_to_neck < 5:
                # 刚突破颈线 5% 以内 → 仍在安全区
                urgency += 3
                reasons.append(f"刚突破颈线{pct_to_neck:.1f}%")
            elif pct_to_neck < 10:
                # 突破 5-10% → 次优
                urgency += 2
                reasons.append(f"突破颈线{pct_to_neck:.1f}%")
            elif pct_to_neck < 15:
                # 突破 10-15% → 偏晚但还有空间
                urgency += 1
                reasons.append(f"已突破{pct_to_neck:.1f}%")

        # 2. 今日适中涨幅（1-4%最佳，不是暴涨）
        if 1 <= change_pct <= 4:
            urgency += 2
            reasons.append(f"稳步上涨{change_pct:+.1f}%")
        elif change_pct > 4:
            # 涨幅过大可能已透支
            urgency -= 1
            if pct_to_neck is None or pct_to_neck > 10:
                # 既暴涨又远离颈线 → 不追
                continue
            reasons.append(f"加速{change_pct:+.1f}%")

        # 3. W底形态加分
        if pattern_score >= 60:
            urgency += 1
        if pattern_score >= 80:
            urgency += 1

        # 4. 温斯坦综合分加分
        if weinstein_score >= 60:
            urgency += 1

        if urgency < 3:
            continue

        level = "🔥 紧急" if urgency >= 6 else "⭐ 关注"
        alerts.append(
                {
                    "symbol": symbol,
                    "name": str(row.get("name", rt.get("name", ""))),
                    "price": f"{price:.2f}",
                    "change_pct": f"{change_pct:+.2f}%",
                    "neckline": f"{neckline:.2f}" if neckline > 0 else "--",
                    "pct_to_neck": f"{pct_to_neck:+.1f}%" if pct_to_neck is not None else "--",
                    "pattern_score": f"{pattern_score:.0f}" if pattern_score else "--",
                    "weinstein_score": f"{weinstein_score:.0f}" if weinstein_score else "--",
                    "urgency": urgency,
                    "level": level,
                    "reasons": " | ".join(reasons),
                }
            )

    # 按紧迫度排序
    alerts.sort(key=lambda a: -a["urgency"])

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_status": "收盘前" if datetime.now().hour == 14 and datetime.now().minute >= 50 else "盘中",
        "total_alerts": len(alerts),
        "alerts": alerts,
    }


def format_push_message(result: dict[str, object]) -> str:
    """格式化推送消息"""
    lines = [
        "📊 **尾盘关注 · 实时选股**",
        f"⏱ {result['timestamp']} ｜ 共 {result['total_alerts']} 只信号",
        "",
    ]

    urgent = [a for a in result["alerts"] if a["level"] == "🔥 紧急"]
    normal = [a for a in result["alerts"] if a["level"] != "🔥 紧急"]

    if urgent:
        lines.append("🔥 **紧急关注（今日加速突破）**")
        lines.append("")
        lines.append("| 代码 | 名称 | 现价 | 涨幅 | 距颈线 | 理由 |")
        lines.append("|------|------|------|------|--------|------|")
        for a in urgent[:8]:
            lines.append(
                f"| {a['symbol']} | {a['name']} | {a['price']} | {a['change_pct']} | {a['pct_to_neck']} | {a['reasons']} |"
            )
        lines.append("")

    if normal:
        lines.append("⭐ **关注（突破前埋伏）**")
        lines.append("")
        lines.append("| 代码 | 名称 | 现价 | 涨幅 | 距颈线 | 理由 |")
        lines.append("|------|------|------|------|--------|------|")
        for a in normal[:10]:
            lines.append(
                f"| {a['symbol']} | {a['name']} | {a['price']} | {a['change_pct']} | {a['pct_to_neck']} | {a['reasons']} |"
            )

    if not result["alerts"]:
        lines.append("今日暂无符合条件的尾盘候选。")

    return "\n".join(lines)


def main() -> None:
    config = load_config(CONFIG_PATH)
    service = DashboardService(CONFIG_PATH)

    # 1. 获取候选股票池（先拿symbol列表再查实时行情）
    print("[preclose] Loading candidates...")
    results = service.get_results()
    candidates = get_candidates(service)
    print(f"[preclose] Candidate pool: {len(candidates)} stocks")

    # 2. 获取实时行情（只查候选股）
    symbol_list = []
    for _, row in candidates.iterrows():
        s = str(row.get("symbol", ""))
        if not s:
            continue
        # 转腾讯格式: 600519.SH -> sh600519
        parts = s.split(".")
        if len(parts) == 2:
            symbol_list.append(f"{parts[1].lower()}{parts[0]}")
        else:
            symbol_list.append(s)

    print(f"[preclose] Fetching real-time data for {len(symbol_list)} stocks...")
    realtime = fetch_realtime(symbol_list)
    print(f"[preclose] Got {len(realtime)} real-time quotes")

    # 3. 获取推荐候选（含W底检测）
    print("[preclose] Computing recommendations...")
    recommendations = compute_recommendations(results, config.parquet_root)
    print(f"[preclose] Got {len(recommendations)} recommendations")

    # 4. 计算尾盘关注信号
    result = compute_intraday_alert(candidates, recommendations, realtime)

    # 5. 保存到文件
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = OUTPUT_DIR / f"pre_close_{ts}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[preclose] Saved to {out_path}")

    # 6. 输出推送消息
    msg = format_push_message(result)
    print("\n" + "=" * 50)
    print(msg)
    print("=" * 50)

    # 输出到 stdout 供 cronjob 捕获
    print("\n---PUSH_MESSAGE_START---")
    print(msg)
    print("---PUSH_MESSAGE_END---")


if __name__ == "__main__":
    main()
