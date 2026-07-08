"""
推荐评分引擎
为已完成的分析任务计算综合评分、推荐理由与投资期限。
推荐的生成与写库由 auto_analyze_and_recommend.py 负责（本模块只做纯计算）。
"""
from typing import Tuple
from app.database import AnalysisTask


def calculate_composite_score(task: AnalysisTask) -> Tuple[float, str, str]:
    """
    计算综合评分并判断投资期限

    优先使用增强分析结果（composite_score），如果没有则使用传统评分

    返回: (综合评分, 推荐理由, 投资期限)
    """
    # 如果有增强分析的综合评分，直接使用
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
