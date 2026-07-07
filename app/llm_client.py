"""
LLM 客户端
支持 OpenAI 兼容格式的 API 调用
配置来源：app.config（已统一加载 .env / .env.llm 与环境变量）
"""
import json
import requests
from typing import Dict, Any, Optional
from pathlib import Path

from app.config import LLM_API_BASE, LLM_API_KEY, LLM_MODEL, LLM_TIMEOUT


class LLMClient:
    """LLM 客户端"""

    def __init__(self, api_base: str = None, api_key: str = None, model: str = None):
        self.api_base = (api_base or LLM_API_BASE).rstrip("/")
        self.api_key = api_key or LLM_API_KEY
        self.model = model or LLM_MODEL

    def available(self) -> bool:
        """LLM 是否已配置（未配置时调用方应走降级路径，而非发起必然失败的请求）"""
        return bool(self.api_base and self.api_key and self.model)

    def call(self, system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: int = 4000) -> Optional[str]:
        """
        调用 LLM API

        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            temperature: 温度参数
            max_tokens: 最大token数

        Returns:
            LLM 返回的文本；未配置或失败返回 None
        """
        if not self.available():
            print("   ℹ️  LLM 未配置（LLM_API_BASE/LLM_API_KEY/LLM_MODEL），跳过调用")
            return None

        try:
            url = f"{self.api_base}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }

            data = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": temperature,
                "max_tokens": max_tokens
            }

            print(f"   → 调用 LLM API: {url}")
            print(f"   → 模型: {self.model}")

            response = requests.post(url, headers=headers, json=data, timeout=LLM_TIMEOUT)
            response.raise_for_status()

            result = response.json()
            content = result["choices"][0]["message"]["content"]
            print(f"   ✓ LLM 返回内容长度: {len(content)} 字符")
            return content

        except requests.exceptions.Timeout:
            print(f"   ❌ LLM API 超时（{LLM_TIMEOUT}秒）")
            return None
        except requests.exceptions.RequestException as e:
            print(f"   ❌ LLM API 调用失败: {e}")
            resp = getattr(e, "response", None)
            if resp is not None and getattr(resp, "text", None):
                print(f"   响应内容: {resp.text[:200]}")
            return None
        except Exception as e:
            print(f"   ❌ LLM 调用异常: {e}")
            return None

    def call_with_skill(self, skill_md_path: Path, context: Dict[str, Any]) -> Optional[str]:
        """
        使用 SKILL.md 调用 LLM

        Args:
            skill_md_path: SKILL.md 文件路径
            context: 上下文数据（如股票代码、名称等）

        Returns:
            LLM 分析结果
        """
        if not skill_md_path.exists():
            print(f"❌ SKILL.md 不存在: {skill_md_path}")
            return None

        # 读取 SKILL.md
        skill_content = skill_md_path.read_text(encoding="utf-8")

        # 构建系统提示词
        system_prompt = f"""你是一个专业的股票分析助手。

请严格按照以下 SKILL 定义执行分析任务：

{skill_content}

输出要求：
- 严格遵循 SKILL.md 中定义的输出格式
- 如果要求输出 JSON，必须返回合法的 JSON 格式
- 分析要客观、准确、有依据
"""

        # 构建用户提示词
        user_prompt = f"""请分析以下股票：

{json.dumps(context, ensure_ascii=False, indent=2)}

请按照 SKILL 定义执行完整的分析流程。"""

        return self.call(system_prompt, user_prompt, temperature=0.3, max_tokens=8000)


# 全局客户端实例
llm_client = LLMClient()
