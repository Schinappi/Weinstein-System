from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json

import pandas as pd
import requests

from winstan.config import AppConfig
from winstan.outputs.explanations import build_weinstein_analysis


class DeepSeekError(RuntimeError):
    pass


def build_detail_analysis(row: pd.Series, config: AppConfig) -> str:
    fallback = build_weinstein_analysis(row, config)
    if not config.llm.enabled:
        return fallback
    if config.llm.provider.lower() != "deepseek":
        return fallback
    api_key = config.llm.api_key
    if not api_key:
        return fallback

    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(row)
    payload = {
        "model": config.llm.model,
        "temperature": config.llm.temperature,
        "max_tokens": config.llm.max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    url = f"{config.llm.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=config.llm.timeout_seconds,
        )
    except requests.RequestException as exc:
        raise DeepSeekError(str(exc)) from exc
    if response.status_code >= 400:
        raise DeepSeekError(_extract_error_message(response))
    try:
        data = response.json()
    except ValueError as exc:
        raise DeepSeekError("DeepSeek 返回了无法解析的响应") from exc
    content = _extract_content(data)
    if not content:
        raise DeepSeekError("DeepSeek 未返回有效分析内容")
    return content


def _build_system_prompt() -> str:
    return """角色设定：
你现在是一名专注A股趋势交易的资深研究员，严格采用 Stan Weinstein（斯坦·温斯坦）阶段分析框架进行判断。你的任务不是泛泛而谈，而是基于系统提供的结构化指标，对单只股票生成专业、克制、可执行的中文分析。

【分析边界（硬规则，必须执行）】
1) 你只能基于输入中已经提供的字段进行分析，禁止编造任何未提供的数据、事件、消息、K线细节、成交明细、基本面或行业信息。
2) 如果某项关键信息缺失，必须明确说明“当前数据不足以确认”，不得脑补。
3) 你的判断必须围绕温斯坦框架展开，包括但不限于：
   - 当前所处阶段（Stage I / II / III / IV）
   - 30周线位置与趋势
   - 10周线与30周线关系
   - 基底是否收敛
   - 是否接近或完成突破
   - 量能是否确认
   - 相对强弱是否达标
   - 上方阻力与风险情况
4) 必须显式结合四分制评分进行解释：
   - Structure Score：结构是否健康、趋势是否漂亮
   - Timing Score：是否处于合适的介入观察区
   - Strength Score：是否属于市场中的强者
   - Risk Score：当前主要风险是否偏高
   - Final Score：综合权衡后的结果
5) 如果综合条件不足，必须明确指出短板在哪里，而不是给模糊乐观结论。
6) 不得输出“必涨”“确定买入”“马上满仓”等绝对化表达。
7) 输出风格应像成熟交易员或研究员：
   - 先结论，后依据
   - 少空话，少套话
   - 多用交易语言，如“可跟踪”“需等待确认”“不宜追高”“仍属观察阶段”
8) 你的建议必须符合A股趋势交易语境，避免超短线盘口指令。若条件尚未成熟，应以“观察、等待、确认”为主。
9) 若股票不符合温斯坦做多框架，应明确指出其不符合之处，而不是强行解释为机会。
10) 结论必须与输入字段一致，尤其不能和 stage_label、stage2_candidate、breakout_status、market_ok、reject_reason、四分制分数相冲突。

【核心分析任务】
你需要根据输入的单股结构化数据，完成以下判断：
- 趋势阶段：这只股票当前更接近哪个温斯坦阶段，属于趋势确认完成还是仍在迁移中
- 交易状态：当前更接近“临近突破”“突破确认”“等待确认”“不宜追高”还是“仍在突破位下方”
- 候选等级：当前更适合定义为“核心候选”“强观察”“普通观察”还是“仅跟踪”
- 当前最主要的风险点是什么
- 接下来最值得观察的触发条件是什么
- 四分制各自为什么高或低，以及这些分数如何支撑上述三层结论

【输出要求（必须执行）】
请按以下结构输出，使用简洁中文 Markdown：

- 使用二级标题组织段落，例如：`## 一句话结论`
- 如有要点，优先使用无序列表
- 不要输出表格
- 不要输出代码块

输出结构：
1. 趋势阶段
2. 交易状态
3. 候选等级
4. 主要风险
5. 操作关注

补充要求：
- 每部分1到3句
- 必须明确区分“趋势阶段”和“交易状态”，禁止把 Stage II 直接等同于可以买
- 必须引用四分制分数来解释判断
- 若存在 reject_reason，必须在候选等级或风险中明确体现
- 若 breakout_status 为 near_breakout / just_broke_out / extended_breakout / below_breakout，应在交易状态中体现
- 若 market_ok 为 False，应在主要风险或操作关注中降低信号权重
- 不要复述字段，不要逐项念数据。你需要像看完一张研究卡片后的交易员一样，提炼出最关键的判断。语言应简洁、克制、专业，避免空泛形容词。"""


def _build_user_prompt(row: pd.Series) -> str:
    payload = {
        "股票信息": {
            "股票代码": _clean_value(row.get("symbol")),
            "股票名称": _clean_value(row.get("name")),
            "交易日期": _clean_value(row.get("trade_date")),
            "收盘价": _clean_value(row.get("close")),
        },
        "阶段与趋势结构": {
            "当前阶段": _clean_value(row.get("stage_label")),
            "阶段II候选": _clean_value(row.get("stage2_candidate")),
            "趋势说明": _clean_value(row.get("stage2_reason")),
            "10周线": _clean_value(row.get("ma_10w")),
            "30周线": _clean_value(row.get("ma_30w")),
            "距30周线%": _clean_value(row.get("price_vs_ma_pct")),
            "10/30周均线差%": _clean_value(row.get("ma_spread_pct")),
            "趋势评分基础": _clean_value(row.get("trend_score")),
            "基底是否平整": _clean_value(row.get("base_flatness_ok")),
            "基底区间%": _clean_value(row.get("base_range_pct")),
            "基底波动%": _clean_value(row.get("base_close_std_pct")),
        },
        "量价与相对强弱": {
            "量能比": _clean_value(row.get("volume_ratio")),
            "量能是否确认": _clean_value(row.get("volume_ok")),
            "量能说明": _clean_value(row.get("volume_reason")),
            "RS排名%": _clean_value(row.get("rs_rank_pct")),
            "RS综合强度": _clean_value(row.get("rs_composite")),
            "RS是否达标": _clean_value(row.get("rs_ok")),
        },
        "阻力与突破": {
            "最近阻力位": _clean_value(row.get("nearest_resistance")),
            "上方空间%": _clean_value(row.get("headroom_pct")),
            "阻力是否通过": _clean_value(row.get("resistance_ok")),
            "突破位": _clean_value(row.get("breakout_level")),
            "距突破位%": _clean_value(row.get("breakout_pct")),
            "突破状态": _clean_value(row.get("breakout_status")),
            "突破是否通过": _clean_value(row.get("breakout_ok")),
        },
        "市场过滤与限制": {
            "市场过滤是否通过": _clean_value(row.get("market_ok")),
            "拒绝原因": _clean_value(row.get("reject_reason")),
        },
        "四分制评分": {
            "综合分": _clean_value(row.get("final_score")),
            "结构分": _clean_value(row.get("structure_score")),
            "时机分": _clean_value(row.get("timing_score")),
            "强度分": _clean_value(row.get("strength_score")),
            "风险分": _clean_value(row.get("risk_score")),
        },
    }
    return "请基于以下温斯坦系统输出的结构化数据，生成一份单股趋势分析。\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def _clean_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, bool):
        return value
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return _format_number(value)
    return str(value)


def _format_number(value: object) -> float | int:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return float(value)
    normalized = number.quantize(Decimal("0.01"))
    if normalized == normalized.to_integral():
        return int(normalized)
    return float(normalized)


def _extract_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or f"DeepSeek 请求失败：HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)
        message = payload.get("message")
        if message:
            return str(message)
    return f"DeepSeek 请求失败：HTTP {response.status_code}"


def _extract_content(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(part.strip() for part in parts if part.strip()).strip()
    return ""
