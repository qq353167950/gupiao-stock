"""
增强分析引擎
集成 deep-analysis + trap-detector + lhb-analyzer 多维度分析

数据真实性原则：
- trap-detector：优先调用 skill 内 fetch_trap_signals.py（真实 web 搜索 8 信号扫描，自带评级），
  失败时降级 LLM 分析，再失败用默认安全评级
- lhb-analyzer：调用 skill 内 fetch_lhb.py（akshare 真实龙虎榜 + 游资席位库），
  基于结构化数据规则评级，不让 LLM 凭空编造
"""
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, Optional

from app.config import (
    get_skill_path,
    PYTHON_EXECUTABLE,
    SKILL_SCRIPTS_DIR,
    SKILL_REPORTS_DIR,
    DEPTH_MAP,
    ANALYSIS_TIMEOUTS,
    FETCHER_TIMEOUT,
    now_cn,
)
from app.llm_client import llm_client


class EnhancedAnalyzer:
    """增强分析器，集成多个UZI-Skill"""

    def __init__(self):
        self.skill_path = get_skill_path()
        self.skills_dir = self.skill_path.parent

    def analyze_stock(self, ticker: str, name: str, depth: str = "standard") -> Dict[str, Any]:
        """
        全流程分析一只股票

        流程：
        1. trap-detector 风险检测（过滤高风险）
        2. deep-analysis 深度分析
        3. lhb-analyzer 龙虎榜分析

        Args:
            ticker: 股票代码
            name: 股票名称
            depth: 分析深度（quick/standard/deep），透传给 deep-analysis

        Returns:
            综合分析结果
        """
        result = {
            "ticker": ticker,
            "name": name,
            "depth": depth,
            "timestamp": now_cn().isoformat(),
            "trap_detection": None,
            "deep_analysis": None,
            "lhb_analysis": None,
            "综合评分": 0,
            "风险等级": "未知",
            "推荐理由": []
        }

        # 阶段1: 风险检测
        print(f"\n{'='*70}")
        print(f"阶段1: {name}({ticker}) - 风险检测")
        print(f"{'='*70}")
        trap_result = self._run_trap_detector(ticker, name)
        result["trap_detection"] = trap_result

        # 如果风险过高，直接返回
        if trap_result and trap_result.get("trap_level") in ["🟠 警惕", "🔴 高度可疑"]:
            result["风险等级"] = trap_result["trap_level"]
            result["综合评分"] = 0
            print(f"⚠️  风险检测未通过: {trap_result['trap_level']}")
            return result

        # 阶段2: 深度分析
        print(f"\n{'='*70}")
        print(f"阶段2: {name}({ticker}) - 深度分析（{depth}）")
        print(f"{'='*70}")
        deep_result = self._run_deep_analysis(ticker, depth)
        result["deep_analysis"] = deep_result

        # 阶段3: 龙虎榜分析
        print(f"\n{'='*70}")
        print(f"阶段3: {name}({ticker}) - 龙虎榜分析")
        print(f"{'='*70}")
        lhb_result = self._run_lhb_analyzer(ticker, name)
        result["lhb_analysis"] = lhb_result

        # 综合评分
        result = self._calculate_score(result)

        return result

    # ─── 通用：调用 skill 内 fetcher 脚本并解析 JSON 输出 ───

    def _run_fetcher(self, script_name: str, arg: str) -> Optional[Dict[str, Any]]:
        """调用 skills/deep-analysis/scripts/ 下的 fetcher 脚本，解析其 stdout JSON。

        fetcher 依赖 scripts 目录下的 lib 包，必须以 scripts 为工作目录运行。
        """
        script = SKILL_SCRIPTS_DIR / script_name
        if not script.exists():
            print(f"   ⚠️  fetcher 不存在: {script}")
            return None

        try:
            proc = subprocess.run(
                [PYTHON_EXECUTABLE, str(script), arg],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=FETCHER_TIMEOUT,
                cwd=str(SKILL_SCRIPTS_DIR),
            )
            if proc.returncode != 0:
                print(f"   ⚠️  {script_name} 退出码 {proc.returncode}: {(proc.stderr or '')[:200]}")
                return None

            # stdout 可能夹杂日志行，提取最外层 JSON 对象
            output = proc.stdout or ""
            start = output.find("{")
            end = output.rfind("}")
            if start == -1 or end <= start:
                print(f"   ⚠️  {script_name} 输出中未找到 JSON")
                return None
            return json.loads(output[start:end + 1])

        except subprocess.TimeoutExpired:
            print(f"   ⚠️  {script_name} 超时（{FETCHER_TIMEOUT}s）")
            return None
        except json.JSONDecodeError as e:
            print(f"   ⚠️  {script_name} JSON 解析失败: {e}")
            return None
        except Exception as e:
            print(f"   ⚠️  {script_name} 执行异常: {e}")
            return None

    # ─── 阶段1: trap-detector ───

    def _run_trap_detector(self, ticker: str, name: str) -> Dict[str, Any]:
        """风险检测：真实信号扫描优先，LLM 兜底，最终默认安全"""
        # 首选：skill 内真实 8 信号 web 扫描（自带评级逻辑）
        print(f"   调用 fetch_trap_signals.py 执行真实信号扫描...")
        fetched = self._run_fetcher("fetch_trap_signals.py", ticker)
        if fetched and isinstance(fetched.get("data"), dict):
            data = fetched["data"]
            if data.get("trap_level"):
                print(f"   ✓ 风险检测完成（真实数据）: {data['trap_level']}")
                return {
                    "trap_level": data["trap_level"],
                    "trap_score": data.get("trap_score", 5),
                    "signals_hit": data.get("signals_hit_detail", []),
                    "recommendation": data.get("recommendation", ""),
                    "source": "fetch_trap_signals",
                }

        # 次选：LLM 分析（仅在配置了 LLM 时）
        llm_result = self._run_trap_detector_llm(ticker, name)
        if llm_result:
            return llm_result

        # 兜底：默认安全评级
        print(f"   ⚠️  信号扫描与 LLM 均不可用，使用默认安全评级")
        return self._get_default_trap_result()

    def _run_trap_detector_llm(self, ticker: str, name: str) -> Optional[Dict[str, Any]]:
        """LLM 版风险检测（fetcher 失败时的降级路径）"""
        if not llm_client.available():
            return None
        try:
            system_prompt = """你是A股风险检测专家。分析股票是否存在以下风险信号：
1. 大量账号同时推荐
2. 话术模板化（"即将爆发"等）
3. VIP群引流
4. 基本面与热度脱节

请给出风险评级：
- 🟢 安全（0-1个信号）
- 🟡 注意（2-3个信号）
- 🟠 警惕（4-5个信号）
- 🔴 高度可疑（6+个信号）

输出JSON格式：{"trap_level": "🟢 安全", "trap_score": 9, "recommendation": "说明"}"""

            user_prompt = f"分析股票：{name}（{ticker}）的风险。"

            print(f"   降级：调用 LLM 执行 trap-detector 分析...")
            result_text = llm_client.call(system_prompt, user_prompt, temperature=0.3, max_tokens=300)
            if not result_text:
                return None

            result = self._parse_trap_result(result_text)
            if result:
                result["source"] = "llm"
                print(f"   ✓ 风险检测完成（LLM）: {result.get('trap_level', '未知')}")
            return result

        except Exception as e:
            print(f"   ❌ trap-detector LLM 异常: {e}")
            return None

    def _get_default_trap_result(self) -> Dict[str, Any]:
        """获取默认的风险检测结果（安全）"""
        return {
            "trap_level": "🟢 安全",
            "trap_score": 9,
            "signals_hit": [],
            "recommendation": "未发现明显推广痕迹，可正常分析",
            "source": "default",
        }

    def _parse_trap_result(self, text: str) -> Optional[Dict[str, Any]]:
        """解析 trap-detector 的 LLM 返回结果"""
        try:
            # 尝试直接解析 JSON
            if text.strip().startswith('{'):
                return json.loads(text)

            # 尝试从文本中提取 JSON
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                return json.loads(json_match.group())

            # 如果没有 JSON，尝试从文本中提取关键信息
            result = {
                "trap_level": "🟢 安全",
                "trap_score": 8,
                "signals_hit": [],
                "recommendation": "基于文本分析"
            }

            # 提取风险等级
            if "🔴" in text or "高度可疑" in text:
                result["trap_level"] = "🔴 高度可疑"
                result["trap_score"] = 2
            elif "🟠" in text or "警惕" in text:
                result["trap_level"] = "🟠 警惕"
                result["trap_score"] = 4
            elif "🟡" in text or "注意" in text:
                result["trap_level"] = "🟡 注意"
                result["trap_score"] = 6

            return result

        except Exception as e:
            print(f"⚠️  解析 trap-detector 结果失败: {e}")
            return None

    # ─── 阶段2: deep-analysis ───

    def _run_deep_analysis(self, ticker: str, depth: str = "standard") -> Optional[Dict[str, Any]]:
        """
        运行deep-analysis深度分析

        调用 run.py 执行分析，深度按 DEPTH_MAP 映射（quick→lite / standard→medium / deep→deep），
        超时按深度从 ANALYSIS_TIMEOUTS 取值。
        """
        try:
            run_script = self.skill_path / "run.py"
            if not run_script.exists():
                print(f"❌ deep-analysis run.py 不存在: {run_script}")
                return None

            skill_depth = DEPTH_MAP.get(depth, "medium")
            timeout = ANALYSIS_TIMEOUTS.get(depth, ANALYSIS_TIMEOUTS["standard"])
            started_at = time.time()

            print(f"   深度: {depth} → --depth {skill_depth}，超时 {timeout}s")

            result = subprocess.run(
                [
                    PYTHON_EXECUTABLE, str(run_script), ticker,
                    "--no-browser",
                    "--depth", skill_depth,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )

            if result.returncode != 0:
                print(f"❌ deep-analysis 执行失败:")
                print((result.stderr or "")[-2000:])
                return None

            output = result.stdout or ""
            print(output[-3000:])

            # 定位报告：扫描 reports 目录（比解析 stdout 更可靠且跨平台）
            report_dir = self._find_report_dir(ticker, since_ts=started_at)
            if report_dir:
                one_liner_score = self._parse_one_liner_score(report_dir)
                return {
                    "report_path": f"{report_dir.name}/full-report-standalone.html",
                    "report_dir": report_dir.name,
                    "score": one_liner_score,
                    "status": "success",
                }

            return {"status": "completed", "output": output[-2000:]}

        except subprocess.TimeoutExpired:
            print(f"❌ deep-analysis 超时（{ANALYSIS_TIMEOUTS.get(depth)}s）")
            return None
        except Exception as e:
            print(f"❌ deep-analysis 执行异常: {e}")
            return None

    def _find_report_dir(self, ticker: str, since_ts: float) -> Optional[Path]:
        """在 skill reports 目录中查找本次分析生成/更新的报告目录。

        匹配策略（严格优先，防止并发任务互相错配）：
        1. 目录名以股票代码开头（如 002310.SZ_20260708）→ 强匹配
        2. 目录 mtime 晚于分析开始时间，且 one-liner 首行以边界匹配含 ticker → 弱匹配
        多个匹配取最新。
        """
        if not SKILL_REPORTS_DIR.exists():
            return None

        import re as _re
        ticker_code = ticker.split('.')[0] if '.' in ticker else ticker
        # 边界匹配：避免 6 位代码作为其他数字（分数/日期/市值）的子串误命中
        code_pattern = _re.compile(rf'(?<![0-9A-Za-z]){_re.escape(ticker_code)}(?![0-9A-Za-z])')
        strong, weak = [], []

        for item in SKILL_REPORTS_DIR.iterdir():
            if not item.is_dir():
                continue
            # 留 60s 余量：resume 模式下报告目录可能在启动前已存在但内容被更新
            if item.stat().st_mtime < since_ts - 60:
                continue
            if ticker_code and item.name.startswith(ticker_code):
                strong.append(item)
                continue
            one_liner = item / "one-liner.txt"
            if one_liner.exists():
                try:
                    first_line = one_liner.read_text(encoding="utf-8").split("\n")[0]
                    if ticker in first_line or code_pattern.search(first_line):
                        weak.append(item)
                except Exception:
                    pass

        candidates = strong or weak
        if not candidates:
            return None

        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        if (latest / "full-report-standalone.html").exists():
            return latest
        return None

    def _parse_one_liner_score(self, report_dir: Path) -> Optional[float]:
        """从 one-liner.txt 提取体检分数（0-100）"""
        one_liner = report_dir / "one-liner.txt"
        if not one_liner.exists():
            return None
        try:
            content = one_liner.read_text(encoding="utf-8")
            match = re.search(r'(\d+)\s*分', content)
            if match:
                return float(match.group(1))
        except Exception:
            pass
        return None

    # ─── 阶段3: lhb-analyzer ───

    def _run_lhb_analyzer(self, ticker: str, name: str) -> Dict[str, Any]:
        """龙虎榜分析：调用 fetch_lhb.py 获取真实数据，规则评级"""
        print(f"   调用 fetch_lhb.py 获取真实龙虎榜数据...")
        fetched = self._run_fetcher("fetch_lhb.py", ticker)

        if not fetched or not isinstance(fetched.get("data"), dict):
            print(f"   ⚠️  龙虎榜数据获取失败，使用默认结果")
            return self._get_default_lhb_result()

        data = fetched["data"]
        if data.get("_note"):  # 非 A 股等跳过场景
            return self._get_default_lhb_result()

        lhb_count = int(data.get("lhb_count_30d", 0) or 0)
        matched_seats = data.get("matched_youzi", []) or []
        split = data.get("inst_vs_youzi", {}) or {}
        inst_net = float(split.get("institutional_net", 0) or 0)
        youzi_net = float(split.get("youzi_net", 0) or 0)

        # 基于真实资金 split 判断主导方
        if lhb_count == 0:
            main_money = "数据不足"
            recommendation = "近30天未上榜，无龙虎榜资金信号"
        elif inst_net > 0 and inst_net >= youzi_net:
            main_money = "机构主导"
            recommendation = f"近30天上榜{lhb_count}次，机构净买入 {inst_net/1e8:.2f} 亿"
        elif youzi_net > 0:
            main_money = "游资主导"
            recommendation = f"近30天上榜{lhb_count}次，游资净买入 {youzi_net/1e8:.2f} 亿"
        else:
            main_money = "资金流出"
            recommendation = f"近30天上榜{lhb_count}次，机构/游资均为净卖出，注意风险"

        result = {
            "recent_lhb_count": lhb_count,
            "main_money": main_money,
            "recommendation": recommendation,
            "inst_net": inst_net,
            "youzi_net": youzi_net,
            "source": "fetch_lhb",
        }
        if matched_seats:
            result["identified_seats"] = matched_seats

        print(f"   ✓ 龙虎榜分析完成（真实数据）: 近30天上榜{lhb_count}次，{main_money}")
        return result

    def _get_default_lhb_result(self) -> Dict[str, Any]:
        """获取默认的龙虎榜结果"""
        return {
            "recent_lhb_count": 0,
            "main_money": "数据不足",
            "recommendation": "暂无龙虎榜数据",
            "source": "default",
        }

    # ─── 综合评分 ───

    def _calculate_score(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        综合评分

        评分逻辑：
        - trap_detector: 风险越低越好（占30%）
        - deep-analysis: 优先用报告体检分（0-100 缩放到 0-50），无分数时完成即得基础分40（占50%）
        - lhb-analyzer: 真实资金支撑评分（占20%）
        """
        score = 0.0
        reasons = []

        # 1. 风险检测评分（占30%）
        trap = result.get("trap_detection")
        if trap:
            trap_score = trap.get("trap_score", 5)
            score += trap_score * 3  # 最高30分
            if trap_score >= 8:
                reasons.append(f"风险检测通过({trap['trap_level']})")
            elif trap_score >= 6:
                reasons.append(f"风险中等({trap['trap_level']})")
            else:
                reasons.append(f"风险较高({trap['trap_level']})")

        # 2. 深度分析评分（占50%）
        deep = result.get("deep_analysis")
        if deep and deep.get("status") == "success":
            deep_score = deep.get("score")
            if isinstance(deep_score, (int, float)):
                score += min(50.0, deep_score * 0.5)
                reasons.append(f"深度体检 {deep_score:.0f} 分")
            else:
                score += 40  # 报告生成但分数未解析，给基础分
                reasons.append("深度分析完成")

        # 3. 龙虎榜评分（占20%）
        lhb = result.get("lhb_analysis")
        if lhb:
            lhb_count = lhb.get("recent_lhb_count", 0)
            main_money = lhb.get("main_money", "")
            if lhb_count > 0 and main_money != "资金流出":
                # 上榜次数评分（最高15分）
                lhb_score = min(15, lhb_count * 3)
                score += lhb_score

                # 主力资金加分（最高5分）
                if "机构主导" in main_money:
                    score += 5
                    reasons.append(f"龙虎榜活跃({lhb_count}次，机构主导)")
                elif "游资主导" in main_money:
                    score += 3
                    reasons.append(f"龙虎榜活跃({lhb_count}次，游资主导)")
                else:
                    reasons.append(f"龙虎榜活跃({lhb_count}次)")

                # 识别到知名游资额外加分
                seats = lhb.get("identified_seats", [])
                if seats:
                    reasons.append(f"发现知名游资: {', '.join(seats[:3])}")
            elif main_money == "资金流出":
                reasons.append(f"龙虎榜显示资金净流出，谨慎")

        result["综合评分"] = round(min(100.0, score), 1)
        result["推荐理由"] = reasons

        # 确定风险等级
        if result["综合评分"] >= 80:
            result["风险等级"] = "🟢 低风险"
        elif result["综合评分"] >= 60:
            result["风险等级"] = "🟡 中风险"
        elif result["综合评分"] >= 40:
            result["风险等级"] = "🟠 高风险"
        else:
            result["风险等级"] = "🔴 极高风险"

        return result
