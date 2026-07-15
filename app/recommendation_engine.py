"""
推荐评分引擎
为已完成的分析任务计算综合评分、推荐理由与投资期限。
推荐的生成与写库由 auto_analyze_and_recommend.py 负责（本模块只做纯计算）。
"""
import json
from typing import Tuple, Dict, Any, Optional
from app.config import (
    RECOMMENDATION_STYLE,
    RECOMMENDATION_RISK_APPETITE,
    SHORT_TERM_WEIGHT,
    MID_TERM_WEIGHT,
)
from app.database import AnalysisTask


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_enhanced_result(task: AnalysisTask) -> Dict[str, Any]:
    if not task.enhanced_result:
        return {}
    try:
        return json.loads(task.enhanced_result)
    except (TypeError, ValueError):
        return {}


def _score_data_quality(task: AnalysisTask) -> Tuple[str, int, list]:
    missing = []
    has_analysis_score = task.score is not None or task.composite_score is not None
    if not has_analysis_score:
        missing.append("体检分")
    if task.dcf_discount is None:
        missing.append("DCF估值")
    if task.bullish_count is None or task.total_voters is None:
        missing.append("评委投票")
    if not task.enhanced_result:
        missing.append("增强分析")
    if not task.report_path:
        missing.append("HTML报告")

    if len(missing) == 0:
        return "A", 100, missing
    if len(missing) <= 1:
        return "B", 85, missing
    if has_analysis_score and task.enhanced_result:
        return "C", 65, missing
    return "D", 35, missing


def _level_thresholds() -> Tuple[float, float, float]:
    """按风险偏好返回 (强推荐, 推荐, 观察) 的分数阈值。"""
    if RECOMMENDATION_RISK_APPETITE == "aggressive":
        return 72.0, 60.0, 50.0
    if RECOMMENDATION_RISK_APPETITE == "conservative":
        return 82.0, 72.0, 62.0
    return 78.0, 68.0, 58.0


def _level_from_score(score: float, risk_level: Optional[str], data_quality: str) -> str:
    """按绝对分给出质量标签。

    注意：这是"质量信号"而非最终展示层级——最终强推荐/推荐/观察的名额
    由 auto_analyze_and_recommend._generate_recommendations 按相对排名分配，
    保证每天都能填满设定数量。此处只负责识别"必须排除/必须谨慎"的硬约束，
    以及在直接调用（如单股评估）时给出可读的质量分层。

    硬约束（不受相对排名影响）：
    - 数据完整度 D（分析基本失败）→ 回避
    - 极高风险 → 谨慎
    - 高风险且非激进偏好 → 谨慎
    """
    strong_threshold, recommend_threshold, observe_threshold = _level_thresholds()

    # 只有分析基本失败（D 级）才直接回避；不再用 score<50 一刀切，
    # 避免行情整体偏弱时把所有股票判为回避导致零推荐。
    if data_quality == "D":
        return "回避"
    if risk_level and "极高风险" in risk_level:
        return "谨慎"
    if risk_level and "高风险" in risk_level and RECOMMENDATION_RISK_APPETITE != "aggressive":
        return "谨慎"
    if score >= strong_threshold:
        return "强推荐"
    if score >= recommend_threshold:
        return "推荐"
    if score >= observe_threshold:
        return "观察"
    return "谨慎"


def evaluate_recommendation(task: AnalysisTask) -> Dict[str, Any]:
    """免费数据多因子推荐评估，返回最终分、分层、数据完整度和解释。"""
    enhanced = _parse_enhanced_result(task)
    trap = enhanced.get("trap_detection") or {}
    lhb = enhanced.get("lhb_analysis") or {}
    data_quality, data_quality_score, missing = _score_data_quality(task)

    deep_score = _safe_float(task.score, _safe_float(task.composite_score))
    dcf_discount = _safe_float(task.dcf_discount)
    bullish_ratio = (
        task.bullish_count / task.total_voters
        if task.bullish_count is not None and task.total_voters else 0.0
    )
    trap_score = _safe_float(trap.get("trap_score"), 5.0)
    lhb_count = int(_safe_float(lhb.get("recent_lhb_count"), 0))
    main_money = lhb.get("main_money", "")

    if RECOMMENDATION_STYLE == "short_mid":
        total_preference = max(1, SHORT_TERM_WEIGHT + MID_TERM_WEIGHT)
        short_ratio = SHORT_TERM_WEIGHT / total_preference
        mid_ratio = MID_TERM_WEIGHT / total_preference
    elif RECOMMENDATION_STYLE == "short":
        short_ratio, mid_ratio = 1.0, 0.0
    elif RECOMMENDATION_STYLE == "mid":
        short_ratio, mid_ratio = 0.0, 1.0
    else:
        short_ratio, mid_ratio = 0.5, 0.5

    short_weights = {
        "fundamental": 14.0,
        "valuation": 10.0,
        "growth": 13.0,
        "capital": 24.0,
        "trend": 24.0,
        "risk": 8.0,
        "catalyst": 7.0,
    }
    mid_weights = {
        "fundamental": 28.0,
        "valuation": 24.0,
        "growth": 18.0,
        "capital": 10.0,
        "trend": 8.0,
        "risk": 10.0,
        "catalyst": 2.0,
    }

    def mixed_weight(key: str) -> float:
        return short_weights[key] * short_ratio + mid_weights[key] * mid_ratio

    fundamental_weight = mixed_weight("fundamental")
    valuation_weight = mixed_weight("valuation")
    growth_weight = mixed_weight("growth")
    capital_weight = mixed_weight("capital")
    trend_weight = mixed_weight("trend")
    risk_weight = mixed_weight("risk")
    catalyst_weight = mixed_weight("catalyst")

    # ─── 因子归一化（0-1）+ 可得性标记 ───
    # 关键设计：稀缺数据（DCF / 龙虎榜 / 评委投票）缺失时，该因子标记为「不可得」，
    # 不再按 0 分计入——只在「可得因子」范围内按权重归一化。避免普通个股因天然
    # 缺少这些数据而被系统性压低分数、最终全部落入「回避」。
    has_dcf = task.dcf_discount is not None
    has_own_score = task.score is not None
    has_bullish = task.bullish_count is not None and bool(task.total_voters)
    has_lhb = bool(lhb) and lhb.get("recent_lhb_count") is not None
    has_capital_signal = has_bullish or (has_lhb and lhb_count > 0)
    has_trap = bool(trap) and trap.get("trap_score") is not None

    # fundamental：体检分/综合分驱动，几乎总是可得（分析完成即有 composite_score）
    fundamental_ratio = max(0.0, min(1.0, deep_score / 100.0))

    # valuation：DCF 折价驱动，仅在有 DCF 时可得
    valuation_ratio = max(0.0, min(1.0, dcf_discount * 2.5)) if has_dcf else None

    # growth：体检分相对成长空间，仅在有独立体检分时可得
    growth_ratio = max(0.0, min(1.0, (deep_score - 45.0) / 40.0)) if has_own_score else None

    # capital：评委看多 + 龙虎榜资金，两者皆无则不可得
    if has_capital_signal:
        capital_ratio = bullish_ratio * 0.7 + min(0.3, lhb_count * 0.1)
        if "机构主导" in main_money:
            capital_ratio += 0.1
        elif "游资主导" in main_money:
            capital_ratio += 0.05
        elif "资金流出" in main_money:
            capital_ratio -= 0.15
        capital_ratio = max(0.0, min(1.0, capital_ratio))
    else:
        capital_ratio = None

    # trend：龙虎榜活跃度 + 看多共识，两者皆无则不可得
    if has_capital_signal:
        trend_ratio = 0.35 + min(0.4, lhb_count * 0.12) + (0.25 if bullish_ratio >= 0.45 else 0.0)
        trend_ratio = max(0.0, min(1.0, trend_ratio))
    else:
        trend_ratio = None

    # risk：trap-detector 风险信号，有默认安全兜底，通常可得
    risk_ratio = max(0.0, min(1.0, trap_score / 10.0)) if has_trap else None

    # catalyst：龙虎榜上榜 + 看多，两者皆无则不可得
    if has_capital_signal:
        catalyst_ratio = min(1.0, lhb_count * 0.25 + (0.3 if bullish_ratio >= 0.5 else 0.0))
    else:
        catalyst_ratio = None

    ratio_by_key = {
        "fundamental": fundamental_ratio,
        "valuation": valuation_ratio,
        "growth": growth_ratio,
        "capital": capital_ratio,
        "trend": trend_ratio,
        "risk": risk_ratio,
        "catalyst": catalyst_ratio,
    }
    weight_by_key = {
        "fundamental": fundamental_weight,
        "valuation": valuation_weight,
        "growth": growth_weight,
        "capital": capital_weight,
        "trend": trend_weight,
        "risk": risk_weight,
        "catalyst": catalyst_weight,
    }

    # 仅在可得因子范围内加权归一化 → 0-100
    available_weight = sum(weight_by_key[k] for k, r in ratio_by_key.items() if r is not None)
    if available_weight > 0:
        weighted = sum(
            weight_by_key[k] * r for k, r in ratio_by_key.items() if r is not None
        )
        factor_composite = weighted / available_weight * 100.0
    else:
        factor_composite = deep_score

    # 与 enhanced_analyzer 已算好的综合分融合：因子模型为主(0.55)、综合分为锚(0.45)。
    # 综合分只要分析成功即有值，为分数提供稳定基准，避免单纯因子模型在数据稀疏时漂移。
    anchor_score = _safe_float(task.composite_score, deep_score)
    raw_score = 0.55 * factor_composite + 0.45 * anchor_score

    penalties = []
    # 数据完整度惩罚保留但大幅收敛：可得因子归一化已消化了「缺数据」的影响，
    # 这里只做温和提示性扣分，不再把股票直接打到推荐线以下。
    if data_quality == "C":
        raw_score -= 2 if RECOMMENDATION_RISK_APPETITE == "aggressive" else 4
        penalties.append("核心数据不完整")
    elif data_quality == "D":
        raw_score -= 8 if RECOMMENDATION_RISK_APPETITE == "aggressive" else 12
        penalties.append("关键数据缺失")
    if task.risk_level and "极高风险" in task.risk_level:
        raw_score -= 20
        penalties.append("极高风险")
    elif task.risk_level and "高风险" in task.risk_level:
        raw_score -= 6 if RECOMMENDATION_RISK_APPETITE == "aggressive" else 10
        penalties.append("高风险")
    if task.score is not None and task.score < 45:
        raw_score -= 8
        penalties.append("体检分偏低")

    # 因子实际得分（用于展示与解释，缺失因子记 0 便于前端对齐 7 列）
    fundamental = fundamental_ratio * fundamental_weight
    valuation = (valuation_ratio or 0.0) * valuation_weight
    growth = (growth_ratio or 0.0) * growth_weight
    capital = (capital_ratio or 0.0) * capital_weight
    trend = (trend_ratio or 0.0) * trend_weight
    risk = (risk_ratio or 0.0) * risk_weight
    catalyst = (catalyst_ratio or 0.0) * catalyst_weight

    final_score = round(max(0.0, min(100.0, raw_score)), 1)
    level = _level_from_score(final_score, task.risk_level, data_quality)

    include_reason = []
    if fundamental_ratio >= 0.72:
        include_reason.append("基本面质量较好")
    if valuation_ratio is not None and valuation_ratio >= 0.6:
        include_reason.append(f"估值具备安全边际({int(dcf_discount * 100)}%)")
    if capital_ratio is not None and capital_ratio >= 0.5:
        include_reason.append("资金认可度较高")
    if trend_ratio is not None and trend_ratio >= 0.65:
        include_reason.append("趋势与活跃度较好")
    if risk_ratio is not None and risk_ratio >= 0.72:
        include_reason.append("风险信号可控")

    exclude_reason = None
    if level in ("回避", "谨慎"):
        reasons = penalties[:]
        if data_quality in ("C", "D") and missing:
            reasons.append("缺少" + "、".join(missing[:3]))
        if final_score < 60:
            reasons.append("综合分未达推荐线")
        exclude_reason = "；".join(dict.fromkeys(reasons)) or "综合条件不足"

    factor_scores = {
        "fundamental": round(fundamental, 1),
        "valuation": round(valuation, 1),
        "growth": round(growth, 1),
        "capital": round(capital, 1),
        "trend": round(trend, 1),
        "risk": round(risk, 1),
        "catalyst": round(catalyst, 1),
    }

    return {
        "final_score": final_score,
        "recommendation_level": level,
        "investment_period": determine_investment_period(task),
        "data_quality": data_quality,
        "data_quality_score": data_quality_score,
        "missing_data": missing,
        "factor_scores": factor_scores,
        "include_reason": include_reason or ["综合表现相对靠前"],
        "exclude_reason": exclude_reason,
    }


def calculate_composite_score(task: AnalysisTask) -> Tuple[float, str, str]:
    """
    计算综合评分并判断投资期限

    优先使用增强分析结果（composite_score），如果没有则使用传统评分

    返回: (综合评分, 推荐理由, 投资期限)
    """
    evaluation = evaluate_recommendation(task)
    score = evaluation["final_score"]
    reason_parts = [evaluation["recommendation_level"], f"数据完整度{evaluation['data_quality']}"]
    reason_parts.extend(evaluation["include_reason"][:3])
    if evaluation.get("exclude_reason"):
        reason_parts.append(evaluation["exclude_reason"])
    return score, " · ".join(reason_parts), evaluation["investment_period"]


def calculate_legacy_composite_score(task: AnalysisTask) -> Tuple[float, str, str]:
    """旧版评分逻辑，保留给回归对比使用。"""
    if task.composite_score is not None:
        score = task.composite_score
        reason_parts = []

        # 根据风险等级添加说明
        if task.risk_level:
            if "低风险" in task.risk_level:
                reason_parts.append("风险较低")
            elif "中风险" in task.risk_level:
                reason_parts.append("风险适中")
            elif "高风险" in task.risk_level or "极高风险" in task.risk_level:
                reason_parts.append("需关注风险")

        # 添加基本面评价
        if task.score is not None:
            if task.score >= 70:
                reason_parts.append("基本面优秀")
            elif task.score >= 60:
                reason_parts.append("基本面良好")

        # 添加估值评价
        if task.dcf_discount and task.dcf_discount > 0.3:
            reason_parts.append(f"DCF低估{int(task.dcf_discount*100)}%")

        # 判断投资期限
        investment_period = determine_investment_period(task)

        return score, " · ".join(reason_parts) if reason_parts else "综合表现", investment_period

    # 传统评分逻辑（增强分析缺失时的后备路径）
    score = 0.0
    reason_parts = []

    # 1. 基本面得分（权重40%）
    if task.score is not None:
        fundamental_score = task.score * 0.4
        score += fundamental_score

        if task.score >= 70:
            reason_parts.append("基本面优秀")
        elif task.score >= 60:
            reason_parts.append("基本面良好")

    # 2. 估值吸引力（权重30%）
    if task.dcf_discount is not None:
        # DCF低估越多，分数越高
        valuation_score = min(task.dcf_discount * 30, 30)
        score += valuation_score

        if task.dcf_discount > 0.5:
            reason_parts.append(f"DCF低估{int(task.dcf_discount*100)}%")
        elif task.dcf_discount > 0.2:
            reason_parts.append(f"估值合理偏低")

    # 3. 评委看多比例（权重20%）
    if task.bullish_count and task.total_voters:
        bullish_ratio = task.bullish_count / task.total_voters
        consensus_score = bullish_ratio * 20
        score += consensus_score

        if bullish_ratio > 0.4:
            reason_parts.append(f"评委看多{int(bullish_ratio*100)}%")

    # 4. 风险调整（权重10%）
    # 如果有杀猪盘风险或严重问题，降低评分
    if task.score is not None and task.score < 40:
        score *= 0.8  # 降低20%
        reason_parts.append("需关注风险")
    else:
        score += 10  # 无明显风险，加10分

    # 判断投资期限
    investment_period = determine_investment_period(task)

    return score, " · ".join(reason_parts) if reason_parts else "综合表现", investment_period


def determine_investment_period(task: AnalysisTask) -> str:
    """
    判断投资期限

    长期：基本面优秀 + DCF低估 > 30%
    短期：基本面一般 + DCF低估 < 30% + 有催化剂
    """
    is_good_fundamental = task.score and task.score >= 65
    is_deeply_undervalued = task.dcf_discount and task.dcf_discount > 0.3

    if is_good_fundamental and is_deeply_undervalued:
        return "长期"
    elif is_good_fundamental:
        return "中长期"
    else:
        return "短期"
