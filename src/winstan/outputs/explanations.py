from __future__ import annotations

import pandas as pd

from winstan.config import AppConfig

STAGE_LABELS = {
    "I": "阶段I（筑底/观察期）",
    "II": "阶段II（上升阶段）",
    "III": "阶段III（顶部整理）",
    "IV": "阶段IV（下跌阶段）",
    "UNKNOWN": "未知阶段",
}

BREAKOUT_LABELS = {
    "just_broke_out": "刚突破",
    "near_breakout": "临近突破",
    "extended_breakout": "突破后偏离过大",
    "below_breakout": "仍在突破位下方",
    "no_breakout_level": "暂无明确突破位",
}

REJECT_REASON_LABELS = {
    "market": "大盘过滤",
    "stage": "阶段结构",
    "volume": "量能确认",
    "relative_strength": "相对强弱",
    "resistance": "上方阻力",
    "breakout": "突破确认",
}


def with_weinstein_analysis(frame: pd.DataFrame, config: AppConfig) -> pd.DataFrame:
    enriched = frame.copy()
    if enriched.empty:
        enriched["温斯坦分析"] = pd.Series(dtype="object")
        return enriched
    enriched["温斯坦分析"] = enriched.apply(lambda row: build_weinstein_analysis(row, config), axis=1)
    return enriched


def build_stock_analysis_report(results: pd.DataFrame, config: AppConfig) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame(
            columns=[
                "交易日期",
                "股票代码",
                "股票名称",
                "阶段",
                "收盘价",
                "市场过滤",
                "阶段II候选",
                "综合分",
                "结构分",
                "时机分",
                "强度分",
                "风险分",
                "量能比",
                "RS排名%",
                "上方空间%",
                "距30周线%",
                "距突破位%",
                "拒绝原因",
                "温斯坦分析",
            ]
        )

    display = with_weinstein_analysis(results, config)
    display["阶段"] = display.apply(lambda row: get_trend_stage_label(row, config), axis=1)
    display["市场过滤"] = display["market_ok"].map({True: "通过", False: "未通过"}).fillna("未知")
    display["阶段II候选"] = display["stage2_candidate"].map({True: "是", False: "否"}).fillna("否")
    display["拒绝原因"] = display["reject_reason"].apply(_format_reject_reason)

    output = display[
        [
            "trade_date",
            "symbol",
            "name",
            "阶段",
            "close",
            "市场过滤",
            "阶段II候选",
            "final_score",
            "structure_score",
            "timing_score",
            "strength_score",
            "risk_score",
            "volume_ratio",
            "rs_rank_pct",
            "headroom_pct",
            "price_vs_ma_pct",
            "breakout_pct",
            "拒绝原因",
            "温斯坦分析",
        ]
    ].rename(
        columns={
            "trade_date": "交易日期",
            "symbol": "股票代码",
            "name": "股票名称",
            "close": "收盘价",
            "final_score": "综合分",
            "structure_score": "结构分",
            "timing_score": "时机分",
            "strength_score": "强度分",
            "risk_score": "风险分",
            "volume_ratio": "量能比",
            "rs_rank_pct": "RS排名%",
            "headroom_pct": "上方空间%",
            "price_vs_ma_pct": "距30周线%",
            "breakout_pct": "距突破位%",
        }
    )
    return output


def build_weinstein_analysis(row: pd.Series, config: AppConfig) -> str:
    sections = [
        ("趋势阶段", _describe_trend_stage(row, config)),
        ("交易状态", _describe_trade_setup(row, config)),
        ("候选等级", _describe_watch_rank(row)),
        ("主要风险", _describe_risk(row, config)),
        ("操作关注", _describe_action(row, config)),
    ]
    return "\n\n".join(f"## {title}\n{content}" for title, content in sections if content)


def get_trend_stage_label(row: pd.Series, config: AppConfig) -> str:
    return str(_trend_stage_profile(row, config)["label"])


def get_watch_rank_label(row: pd.Series) -> str:
    return _watch_rank_profile(row)["label"]


def _describe_trend_stage(row: pd.Series, config: AppConfig) -> str:
    profile = _trend_stage_profile(row, config)
    stage_label = _to_text(row.get("stage_label")) or "UNKNOWN"
    price_vs_ma_pct = _to_float(row.get("price_vs_ma_pct"))
    ma_spread_pct = _to_float(row.get("ma_spread_pct"))

    bits: list[str] = [f"当前更接近{profile['label']}，{profile['summary']}"]

    if price_vs_ma_pct is not None:
        relation = "高于" if price_vs_ma_pct >= 0 else "低于"
        bits.append(f"收盘价{relation}30周线{abs(price_vs_ma_pct):.2f}%")
    if ma_spread_pct is not None:
        bits.append(f"10/30周线差约{ma_spread_pct:.2f}%")
    if stage_label == "II" and not _to_bool(row.get("resistance_ok")):
        bits.append("但上方空间仍限制趋势确认度")
    return "，".join(bit for bit in bits if bit)


def _trend_stage_profile(row: pd.Series, config: AppConfig) -> dict[str, object]:
    stage_label = _to_text(row.get("stage_label")) or "UNKNOWN"
    stage_name = STAGE_LABELS.get(stage_label, stage_label)
    stage2_candidate = _to_bool(row.get("stage2_candidate"))
    base_flatness_ok = _to_bool(row.get("base_flatness_ok"))
    price_vs_ma_pct = _to_float(row.get("price_vs_ma_pct"))
    breakout_status_key = _to_text(row.get("breakout_status")) or "no_breakout_level"

    if stage_label == "II":
        extended = breakout_status_key == "extended_breakout" or (
            price_vs_ma_pct is not None and price_vs_ma_pct > config.strategy.watch_max_price_vs_ma_pct
        )
        confirmed = _is_core_candidate(row)
        if extended:
            return {
                "label": "Extended Stage II（延伸阶段）",
                "summary": "趋势仍强，但价格位置已偏扩展，更接近强势延伸而非理想起涨区",
            }
        if confirmed:
            return {
                "label": "Confirmed Stage II（确认上升）",
                "summary": "趋势结构和关键过滤已较充分确认，属于较成熟的上升阶段",
            }
        if stage2_candidate or base_flatness_ok:
            return {
                "label": "Early Stage II（初升阶段）",
                "summary": "长期结构已经转强，但仍处于由观察走向确认的早期阶段",
            }
        return {
            "label": "Stage II（观察型上升）",
            "summary": "处于上升趋势框架内，但结构确认度与可交易性仍然一般",
        }
    if stage_label == "I":
        if base_flatness_ok:
            return {
                "label": "Stage I（筑底收敛）",
                "summary": "基底已有一定收敛，正处于为后续趋势切换做准备的阶段",
            }
        return {
            "label": "Stage I（筑底阶段）",
            "summary": "仍以筑底和结构整理为主，尚未进入明确上升阶段",
        }
    if stage_label == "III":
        return {
            "label": "Stage III（顶部整理）",
            "summary": "上涨结构开始钝化，更偏向高位整理或分歧加大的阶段",
        }
    if stage_label == "IV":
        return {
            "label": "Stage IV（下跌阶段）",
            "summary": "仍处于弱势下行框架，尚未回到温斯坦做多结构",
        }
    return {
        "label": stage_name,
        "summary": "当前数据或结构不足以完成更细粒度的趋势阶段判断",
    }


def _describe_trade_setup(row: pd.Series, config: AppConfig) -> str:
    breakout_status_key = _to_text(row.get("breakout_status")) or "no_breakout_level"
    breakout_status = BREAKOUT_LABELS.get(breakout_status_key, breakout_status_key)
    breakout_level = _to_float(row.get("breakout_level"))
    breakout_pct = _to_float(row.get("breakout_pct"))
    timing_score = _to_float(row.get("timing_score"))
    structure_score = _to_float(row.get("structure_score"))
    strength_score = _to_float(row.get("strength_score"))
    stage2_watch_reason = _to_text(row.get("stage2_watch_reason")) or _to_text(row.get("stage2_reason"))
    volume_ratio = _to_float(row.get("volume_ratio"))
    rs_rank_pct = _to_float(row.get("rs_rank_pct"))

    bits: list[str] = []
    timing_label = _score_bucket(timing_score)
    structure_label = _score_bucket(structure_score)
    strength_label = _score_bucket(strength_score)

    if breakout_status_key == "just_broke_out":
        bits.append(f"当前属于{breakout_status}，时机{timing_label}")
    elif breakout_status_key == "near_breakout":
        if breakout_level is not None and breakout_pct is not None:
            bits.append(f"当前属于{breakout_status}，距离突破位{breakout_level:.2f}约{abs(breakout_pct):.2f}%，时机{timing_label}")
        else:
            bits.append(f"当前属于{breakout_status}，时机{timing_label}")
    elif breakout_status_key == "extended_breakout":
        bits.append(f"当前属于{breakout_status}，时机{timing_label}，更偏向等待整理而非追价")
    elif breakout_status_key == "below_breakout":
        bits.append(f"当前仍在突破位下方，时机{timing_label}")
    else:
        bits.append(f"当前交易时机评估为{timing_label}")

    bits.append(f"结构{structure_label}、强度{strength_label}")
    if volume_ratio is not None:
        bits.append(f"最新量能约为10周均量的{volume_ratio:.2f}倍")
    if rs_rank_pct is not None:
        bits.append(f"RS位于前{rs_rank_pct:.2f}%")
    if stage2_watch_reason and stage2_watch_reason != "普通趋势":
        bits.append(stage2_watch_reason)
    return "；".join(bit for bit in bits if bit)


def _describe_watch_rank(row: pd.Series) -> str:
    profile = _watch_rank_profile(row)
    rank_label = profile["label"]
    final_score = _to_float(row.get("final_score"))
    reject_reason = _format_reject_reason(row.get("reject_reason"))
    gate_pass_count = int(profile["gate_pass_count"])
    summary = str(profile["summary"])

    bits = [rank_label]
    if final_score is not None:
        bits.append(f"综合分{final_score:.2f}")
    bits.append(f"关键过滤通过{gate_pass_count}/5项")
    if reject_reason:
        bits.append(f"当前短板在{reject_reason}")
    else:
        bits.append(summary)
    return "，".join(bit for bit in bits if bit)


def _describe_risk(row: pd.Series, config: AppConfig) -> str:
    risk_score = _to_float(row.get("risk_score"))
    risk_label = _risk_bucket(risk_score)
    risks: list[str] = []

    if not _to_bool(row.get("market_ok")):
        risks.append("大盘过滤未通过，个股信号需降权")

    headroom_pct = _to_float(row.get("headroom_pct"))
    if not _to_bool(row.get("resistance_ok")):
        if headroom_pct is not None:
            risks.append(f"上方空间仅约{headroom_pct:.2f}%，阻力偏近")
        else:
            risks.append("上方阻力距离偏近")

    if not _to_bool(row.get("volume_ok")):
        risks.append("成交量尚未形成充分确认")

    if not _to_bool(row.get("rs_ok")):
        rs_rank_pct = _to_float(row.get("rs_rank_pct"))
        if rs_rank_pct is not None:
            risks.append(f"相对强弱仅位于前{rs_rank_pct:.2f}%，尚未达到前{config.strategy.rs_rank_threshold_pct}%阈值")
        else:
            risks.append("相对强弱数据不足或未达阈值")

    breakout_status_key = _to_text(row.get("breakout_status")) or "no_breakout_level"
    if breakout_status_key == "extended_breakout":
        risks.append("价格已偏离突破位较多，追高风险上升")
    elif not _to_bool(row.get("breakout_ok")) and breakout_status_key not in {"near_breakout", "just_broke_out"}:
        risks.append("突破条件尚未确认")

    stage_label = _to_text(row.get("stage_label")) or "UNKNOWN"
    if stage_label == "III":
        risks.append("阶段结构偏向顶部整理，趋势延续性不足")
    elif stage_label == "IV":
        risks.append("仍处于弱势下行框架")

    if not risks:
        return f"当前风险评级为{risk_label}，暂未看到突出的结构性短板"
    return f"当前风险评级为{risk_label}，主要来自" + "、".join(risks)


def _watch_rank_profile(row: pd.Series) -> dict[str, object]:
    stage_label = _to_text(row.get("stage_label")) or "UNKNOWN"
    market_ok = _to_bool(row.get("market_ok"))
    breakout_status_key = _to_text(row.get("breakout_status")) or "no_breakout_level"
    final_score = _to_float(row.get("final_score")) or 0.0
    structure_score = _to_float(row.get("structure_score")) or 0.0
    timing_score = _to_float(row.get("timing_score")) or 0.0
    strength_score = _to_float(row.get("strength_score")) or 0.0
    core_gate_keys = ["stage2_candidate", "volume_ok", "rs_ok", "resistance_ok", "breakout_ok"]
    gate_pass_count = sum(1 for key in core_gate_keys if _to_bool(row.get(key)))
    in_watch_zone = breakout_status_key in {"near_breakout", "just_broke_out", "below_breakout"}
    extended = breakout_status_key == "extended_breakout"

    if (
        _is_core_candidate(row)
        and final_score >= 70.0
        and structure_score >= 65.0
        and timing_score >= 60.0
        and strength_score >= 60.0
    ):
        return {
            "label": "核心候选",
            "gate_pass_count": gate_pass_count,
            "summary": "趋势、时机与强度已进入优先处理区，可作为近期重点对象",
        }

    if market_ok and stage_label == "II" and gate_pass_count >= 4 and final_score >= 60.0 and not extended:
        return {
            "label": "强观察",
            "gate_pass_count": gate_pass_count,
            "summary": "趋势和时机具备较强跟踪价值，但仍有1项左右约束未完全通过",
        }

    if stage_label in {"I", "II"} and (gate_pass_count >= 2 or final_score >= 45.0 or in_watch_zone):
        return {
            "label": "普通观察",
            "gate_pass_count": gate_pass_count,
            "summary": "趋势框架仍值得保留观察，但暂未进入优先处理区",
        }

    return {
        "label": "仅跟踪",
        "gate_pass_count": gate_pass_count,
        "summary": "当前更适合作为背景跟踪对象，不宜列为近期重点",
    }


def _is_core_candidate(row: pd.Series) -> bool:
    breakout_status = str(row.get("breakout_status") or "no_breakout_level")
    # 突破期/临近突破的股票天然靠近阻力位，不卡 resistance_ok
    needs_resistance = breakout_status in {"extended_breakout", "no_breakout_level"}
    gates = [
        _to_bool(row.get("market_ok")),
        _to_bool(row.get("stage2_candidate")),
        _to_bool(row.get("volume_ok")),
        _to_bool(row.get("rs_ok")),
        _to_bool(row.get("breakout_ok")),
    ]
    if needs_resistance:
        gates.append(_to_bool(row.get("resistance_ok")))
    return all(gates)


def _describe_stage(row: pd.Series) -> str:
    stage_label = _to_text(row.get("stage_label")) or "UNKNOWN"
    stage_name = STAGE_LABELS.get(stage_label, stage_label)
    stage2_candidate = _to_bool(row.get("stage2_candidate"))
    base_flatness_ok = _to_bool(row.get("base_flatness_ok"))

    if stage_label == "II" and stage2_candidate:
        return f"当前处于{stage_name}，价格、30周线斜率与高低点结构基本满足阶段II候选条件"
    if stage_label == "I":
        suffix = "，基底收敛度较好，偏向温斯坦观察名单" if base_flatness_ok else "，30周线已有改善，但基底仍需继续收敛"
        return f"当前更接近{stage_name}{suffix}"
    if stage_label == "III":
        return f"当前更接近{stage_name}，趋势延续性不足，容易演变为顶部整理或换手区"
    if stage_label == "IV":
        return f"当前处于{stage_name}，股价位于弱势趋势框架内，尚未回到温斯坦做多结构"
    return f"当前处于{stage_name}，数据或趋势结构仍不足以完成清晰定性"


def _describe_trend(row: pd.Series) -> str:
    bits: list[str] = []
    price_vs_ma_pct = _to_float(row.get("price_vs_ma_pct"))
    ma_10w = _to_float(row.get("ma_10w"))
    ma_30w = _to_float(row.get("ma_30w"))
    ma_spread_pct = _to_float(row.get("ma_spread_pct"))
    stage2_reason = _to_text(row.get("stage2_reason")) or "普通趋势"

    if price_vs_ma_pct is not None:
        position = "高于" if price_vs_ma_pct >= 0 else "低于"
        bits.append(f"当前收盘价{position}30周线{abs(price_vs_ma_pct):.2f}%")
    if ma_10w is not None and ma_30w is not None:
        relation = "上方" if ma_10w >= ma_30w else "下方"
        bits.append(f"10周线位于30周线{relation}")
    if ma_spread_pct is not None:
        bits.append(f"10/30周线差约{ma_spread_pct:.2f}%")
    bits.append(stage2_reason)
    return "，".join(bit for bit in bits if bit)


def _describe_volume(row: pd.Series) -> str:
    volume_ratio = _to_float(row.get("volume_ratio"))
    volume_ok = _to_bool(row.get("volume_ok"))
    volume_reason = _to_text(row.get("volume_reason")) or "量能信息不足"

    if volume_ratio is None:
        return volume_reason
    if volume_ratio >= 1.8:
        status = "成交量明显放大"
    elif volume_ratio >= 1.2:
        status = "成交量温和放大"
    elif volume_ratio >= 0.9:
        status = "成交量大致持平"
    else:
        status = "成交量偏弱"
    confirmation = "量能对趋势有确认" if volume_ok else "量能尚未形成充分确认"
    return f"最新周量约为10周均量的{volume_ratio:.2f}倍，{status}，{confirmation}，{volume_reason}"


def _describe_rs(row: pd.Series, config: AppConfig) -> str:
    rs_rank_pct = _to_float(row.get("rs_rank_pct"))
    if rs_rank_pct is None:
        return "暂无可靠的RS排名数据"
    rs_ok = _to_bool(row.get("rs_ok"))
    if rs_rank_pct <= 10:
        level = "属于市场最强一档"
    elif rs_rank_pct <= config.strategy.rs_rank_threshold_pct:
        level = "处于策略允许的强势区间"
    elif rs_rank_pct <= 50:
        level = "强度中性偏上，但还不够强"
    else:
        level = "强度明显不足"
    threshold_text = "满足" if rs_ok else "未达到"
    return f"RS排名位于前{rs_rank_pct:.2f}%，{level}，{threshold_text}当前阈值前{config.strategy.rs_rank_threshold_pct}%的要求"


def _describe_resistance_and_breakout(row: pd.Series) -> str:
    bits: list[str] = []
    nearest_resistance = _to_float(row.get("nearest_resistance"))
    headroom_pct = _to_float(row.get("headroom_pct"))
    breakout_level = _to_float(row.get("breakout_level"))
    breakout_pct = _to_float(row.get("breakout_pct"))
    breakout_status_key = _to_text(row.get("breakout_status")) or "no_breakout_level"
    breakout_status = BREAKOUT_LABELS.get(breakout_status_key, breakout_status_key)

    if nearest_resistance is not None:
        if headroom_pct is not None:
            bits.append(f"最近有效阻力约在{nearest_resistance:.2f}，上方空间约{headroom_pct:.2f}%")
        else:
            bits.append(f"最近有效阻力约在{nearest_resistance:.2f}")
    else:
        bits.append("上方暂未识别出清晰近端阻力")

    if breakout_level is not None:
        if breakout_pct is not None:
            location = "高于" if breakout_pct >= 0 else "低于"
            bits.append(f"突破位约{breakout_level:.2f}，当前{location}突破位{abs(breakout_pct):.2f}%")
        else:
            bits.append(f"突破位约{breakout_level:.2f}")
    else:
        bits.append("暂无明确突破位")

    bits.append(f"当前形态属于“{breakout_status}”")
    return "，".join(bits)


def _describe_conclusion(row: pd.Series) -> str:
    candidate = all(
        [
            _to_bool(row.get("market_ok")),
            _to_bool(row.get("stage2_candidate")),
            _to_bool(row.get("volume_ok")),
            _to_bool(row.get("rs_ok")),
            _to_bool(row.get("resistance_ok")),
            _to_bool(row.get("breakout_ok")),
        ]
    )
    if candidate:
        return "该股已满足当前温斯坦规则引擎的核心做多过滤，可列入阶段II候选重点跟踪"

    reject_reason = _format_reject_reason(row.get("reject_reason"))
    if reject_reason:
        return f"该股暂未进入候选名单，主要短板在{reject_reason}"
    return "该股处于观察阶段，暂未形成足够明确的趋势优势"


def _describe_action(row: pd.Series, config: AppConfig) -> str:
    stage_label = _to_text(row.get("stage_label")) or "UNKNOWN"
    breakout_status = _to_text(row.get("breakout_status")) or "no_breakout_level"
    breakout_level = _to_float(row.get("breakout_level"))
    market_ok = _to_bool(row.get("market_ok"))
    candidate = _to_bool(row.get("stage2_candidate")) and _to_bool(row.get("volume_ok")) and _to_bool(row.get("rs_ok"))

    prefix = "在大盘过滤未通过的情况下，应降低个股信号权重；" if not market_ok else ""

    if stage_label == "II" and candidate:
        if breakout_status == "near_breakout" and breakout_level is not None:
            return prefix + f"可重点观察是否放量突破{breakout_level:.2f}并站稳，若量能不足则等待回踩确认后再评估"
        if breakout_status == "just_broke_out" and breakout_level is not None:
            return prefix + f"已进入突破观察区，优先跟踪突破后对{breakout_level:.2f}附近的回踩承接，防范假突破"
        if breakout_status == "extended_breakout":
            return prefix + "走势虽强，但已偏离突破位较多，按温斯坦框架更适合等整理而不是追高"
        return prefix + "继续跟踪30周线斜率、量能和高低点抬升是否同步维持"

    if stage_label == "I":
        return prefix + "可作为观察股跟踪基底是否继续收窄、10周线上穿30周线，以及是否出现有效放量突破"
    if stage_label == "III":
        return prefix + "更适合等待整理完成或重新收复关键均线后再看，不宜在阻力附近追价"
    if stage_label == "IV":
        return prefix + "暂不符合温斯坦做多框架，应等待30周线走平、股价重新站上均线并形成新基底后再评估"
    return prefix + f"先观察价格能否重新回到30周线附近并满足RS前{config.strategy.rs_rank_threshold_pct}%与量能确认条件"


def _score_bucket(value: float | None) -> str:
    if value is None:
        return "未知"
    if value >= 80.0:
        return "优秀"
    if value >= 65.0:
        return "良好"
    if value >= 50.0:
        return "中等"
    return "偏弱"


def _risk_bucket(value: float | None) -> str:
    if value is None:
        return "未知"
    if value >= 70.0:
        return "较高"
    if value >= 45.0:
        return "可控偏高"
    if value >= 25.0:
        return "可控"
    return "较低"


def _format_reject_reason(value: object) -> str:
    if value is None or pd.isna(value) or str(value).strip() == "":
        return ""
    reasons = []
    for item in str(value).split(","):
        key = item.strip()
        if not key:
            continue
        reasons.append(REJECT_REASON_LABELS.get(key, key))
    return "、".join(reasons)


def _to_float(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _to_bool(value: object) -> bool:
    if value is None or pd.isna(value):
        return False
    return bool(value)


def _to_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value)
