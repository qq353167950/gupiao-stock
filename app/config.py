"""
全局配置模块
- 启动时统一加载 .env / .env.llm 到环境变量（零依赖，复用 UZI-Skill run.py 的加载模式）
- 所有部署相关参数均可通过环境变量覆盖，改配置无需改代码
- now_cn()：全项目统一的北京时间来源（A股业务时间语义与服务器时区解耦）
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── 北京时间（全项目唯一时间源）───
# A股的交易日判断、收盘分析、早盘推送、推荐日期全部绑定北京时间；
# 显式时区计算不依赖服务器 TZ 设置，海外容器/面板注入的任何时区均不影响业务。
try:
    from zoneinfo import ZoneInfo
    TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:
    # 系统缺 IANA 时区数据库（如精简 Windows 且未装 tzdata 包）时兜底：
    # Asia/Shanghai 自 1991 年起无夏令时，固定 UTC+8 完全等价
    TZ_SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now_cn() -> datetime:
    """返回当前北京时间（naive datetime，与数据库既有数据格式一致）。

    去掉 tzinfo 是刻意的：SQLite 中历史数据全部为 naive，保持同构才能
    直接比较、strftime、做 timedelta 运算，无需数据迁移。
    """
    return datetime.now(TZ_SHANGHAI).replace(tzinfo=None)

# 路径配置（必须最先定义，.env 加载依赖 BASE_DIR）
BASE_DIR = Path(__file__).parent.parent
REPORTS_DIR = BASE_DIR / "reports"
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"


def load_env_files():
    """加载 BASE_DIR 下的 .env 与 .env.llm 到 os.environ。

    刻意保持简单（不支持引号嵌套与变量插值），已存在的 shell 环境变量优先，
    因此 `export LLM_API_KEY=...` 始终生效。
    """
    for env_name in (".env", ".env.llm"):
        env_path = BASE_DIR / env_name
        if not env_path.exists():
            continue
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = val
        except Exception as e:
            print(f"⚠️  读取 {env_name} 失败（忽略）: {e}")


load_env_files()


def _env_bool(key: str, default: bool) -> bool:
    """解析布尔型环境变量（true/1/yes 视为真）"""
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("true", "1", "yes", "on")


def _env_int(key: str, default: int) -> int:
    """解析整型环境变量，非法值回退默认"""
    try:
        return int(os.getenv(key, "").strip() or default)
    except (ValueError, AttributeError):
        return default


# 基础配置
APP_NAME = "股票智能分析平台"
VERSION = "1.1.0"
DEBUG = _env_bool("DEBUG", False)
HOST = os.getenv("HOST", "0.0.0.0")
# 端口优先级：PORT > SERVER_PORT（Pterodactyl 等容器面板自动注入）> 8888
PORT = _env_int("PORT", _env_int("SERVER_PORT", 8888))

# 对外访问地址（用于推送消息中的报告链接，如 http://1.2.3.4:8888）
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", f"http://localhost:{PORT}").rstrip("/")

# Python 解释器（子进程统一使用当前解释器，避免服务器上 python3 指向不一致）
PYTHON_EXECUTABLE = sys.executable

# Deep Analysis Skill 配置
SKILL_MODE = os.getenv("SKILL_MODE", "builtin")  # builtin（内置）或 external（外部）

# 外部模式：使用本地路径
EXTERNAL_SKILL_PATH = Path(os.getenv("DEEP_ANALYSIS_PATH", "/root/.opencode/skills/deep-analysis"))

# 内置模式：使用项目内置的 skill
BUILTIN_SKILL_PATH = BASE_DIR / "skills/deep-analysis"

# 自动更新配置（仅内置模式）
AUTO_UPDATE_SKILL = _env_bool("AUTO_UPDATE_SKILL", True)
SKILL_REPO_URL = os.getenv("SKILL_REPO_URL", "https://github.com/wbh604/UZI-Skill.git")  # deep-analysis 官方仓库
SKILL_BRANCH = os.getenv("SKILL_BRANCH", "main")
# 更新检查间隔（天）：每 N 天用 git ls-remote 比对远端 commit，有变化才拉取同步
SKILL_UPDATE_INTERVAL_DAYS = _env_int("SKILL_UPDATE_INTERVAL_DAYS", 3)
# GitHub 镜像代理前缀（大陆服务器直连 GitHub 失败时配置，如 https://gh-proxy.com/）
GITHUB_PROXY = os.getenv("GITHUB_PROXY", "").strip()


def get_skill_path():
    """获取当前使用的 skill 路径"""
    if SKILL_MODE == "external":
        return EXTERNAL_SKILL_PATH
    else:
        return BUILTIN_SKILL_PATH


# Deep Analysis Skill 执行脚本与目录
DEEP_ANALYSIS_PATH = get_skill_path()
DEEP_ANALYSIS_SCRIPT = DEEP_ANALYSIS_PATH / "run.py"
SKILL_SCRIPTS_DIR = DEEP_ANALYSIS_PATH / "scripts"
SKILL_REPORTS_DIR = SKILL_SCRIPTS_DIR / "reports"
SKILL_CACHE_DIR = SKILL_SCRIPTS_DIR / ".cache"

# 数据库配置
DATABASE_URL = f"sqlite:///{DATA_DIR}/analyzer.db"

# 报告配置
REPORT_EXPIRE_HOURS = _env_int("REPORT_EXPIRE_HOURS", 24)  # 报告链接有效期（小时）

# 并发与分析量配置
MAX_CONCURRENT_TASKS = _env_int("MAX_CONCURRENT_TASKS", 3)  # 批量分析（定时推荐）最大并发数
MANUAL_CONCURRENT_TASKS = _env_int("MANUAL_CONCURRENT_TASKS", 2)  # Web 手动分析最大并发数（与批量通道独立）
DAILY_ANALYSIS_TARGET_COUNT = _env_int("DAILY_ANALYSIS_TARGET_COUNT", 50)  # 每日批量分析目标股票数
STOCKS_PER_SECTOR = _env_int("STOCKS_PER_SECTOR", 9)  # 兼容旧配置：每大板块候选数（6 × 9 ≈ 50）
RECOMMENDATION_STYLE = os.getenv("RECOMMENDATION_STYLE", "short_mid").strip().lower()  # short/mid/short_mid/balanced
RECOMMENDATION_RISK_APPETITE = os.getenv("RECOMMENDATION_RISK_APPETITE", "aggressive").strip().lower()  # conservative/balanced/aggressive
SHORT_TERM_WEIGHT = _env_int("SHORT_TERM_WEIGHT", 70)  # 短线偏好权重
MID_TERM_WEIGHT = _env_int("MID_TERM_WEIGHT", 30)  # 中线偏好权重
STRONG_RECOMMEND_LIMIT = _env_int("STRONG_RECOMMEND_LIMIT", 5)  # 强推荐展示上限
RECOMMEND_LIMIT = _env_int("RECOMMEND_LIMIT", 10)  # 推荐展示上限
OBSERVE_LIMIT = _env_int("OBSERVE_LIMIT", 20)  # 观察展示上限

# 分析深度配置
# Web 端深度（quick/standard/deep）→ UZI-Skill run.py --depth（lite/medium/deep）
DEPTH_MAP = {
    "quick": "lite",
    "standard": "medium",
    "deep": "deep",
}

# 各深度的子进程超时（秒）。UZI-Skill 实测：lite 1-2min / medium 5-8min / deep 15-20min，
# 留出网络波动余量
ANALYSIS_TIMEOUTS = {
    "quick": _env_int("ANALYSIS_TIMEOUT_QUICK", 600),
    "standard": _env_int("ANALYSIS_TIMEOUT_STANDARD", 1800),
    "deep": _env_int("ANALYSIS_TIMEOUT_DEEP", 3600),
}

# 辅助 fetcher（trap/lhb 真实数据抓取）超时（秒）
FETCHER_TIMEOUT = _env_int("FETCHER_TIMEOUT", 180)

ANALYSIS_DEPTHS = {
    "quick": {
        "name": "快速分析",
        "estimated_time": 600,
        "description": "10分钟，基础数据 + 关键评委"
    },
    "standard": {
        "name": "标准分析",
        "estimated_time": 1800,
        "description": "30分钟，完整数据 + 50位评委"
    },
    "deep": {
        "name": "深度分析",
        "estimated_time": 3600,
        "description": "60分钟，全维度 + 65位评委"
    }
}

# 定时任务配置（格式 HH:MM，仅交易日执行）
AFTER_MARKET_ANALYSIS_TIME = os.getenv("AFTER_MARKET_ANALYSIS_TIME", "15:10")  # 收盘后分析
MORNING_PUSH_TIME = os.getenv("MORNING_PUSH_TIME", "08:20")  # 早盘推荐推送
CLEANUP_TIME = os.getenv("CLEANUP_TIME", "03:30")  # 每日磁盘清理
DAILY_ANALYSIS_DEPTH = os.getenv("DAILY_ANALYSIS_DEPTH", "standard")  # 每日定时分析深度

# 数据保留配置（天）
REPORT_RETENTION_DAYS = _env_int("REPORT_RETENTION_DAYS", 7)  # 报告目录保留天数
CACHE_RETENTION_DAYS = _env_int("CACHE_RETENTION_DAYS", 3)  # skill .cache 保留天数
LOG_RETENTION_DAYS = _env_int("LOG_RETENTION_DAYS", 30)  # 日志保留天数
TASK_RETENTION_DAYS = _env_int("TASK_RETENTION_DAYS", 90)  # 分析任务记录保留天数

# 推送配置
# 渠道列表（逗号分隔）：serverchan,pushplus,dingtalk,wecom,telegram,email；留空则不推送
NOTIFY_CHANNELS = [
    c.strip().lower()
    for c in os.getenv("NOTIFY_CHANNELS", "").split(",")
    if c.strip()
]
NOTIFY_ON_ANALYSIS_COMPLETE = _env_bool("NOTIFY_ON_ANALYSIS_COMPLETE", True)  # 分析完成后立即推送

# 各渠道凭据（按需填写，见 .env.example）
SERVERCHAN_SENDKEY = os.getenv("SERVERCHAN_SENDKEY", "")
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
DINGTALK_WEBHOOK = os.getenv("DINGTALK_WEBHOOK", "")
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET", "")
WECOM_WEBHOOK = os.getenv("WECOM_WEBHOOK", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = _env_int("SMTP_PORT", 465)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_TO = os.getenv("SMTP_TO", "")  # 收件人，多个用逗号分隔

# LLM 配置（兜底分析用，可空；.env.llm 已由 load_env_files 加载）
LLM_API_BASE = os.getenv("LLM_API_BASE", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_TIMEOUT = _env_int("LLM_TIMEOUT", 120)

# ─── 账号体系配置 ───
# 内置管理员：启动时自动创建（仅当用户名不存在时），用于登录后开设其他账号。
# ⚠️ 部署后请立即用 ADMIN_PASSWORD 环境变量覆盖默认密码，或登录后在界面修改。
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# 会话有效期（秒），默认 7 天
SESSION_MAX_AGE = _env_int("SESSION_MAX_AGE", 7 * 86400)


def load_session_secret() -> str:
    """会话签名密钥：环境变量优先，否则持久化到 data/.session_secret。

    自动生成并落盘（而非每次启动随机）保证服务重启后已登录会话不失效。
    """
    secret = os.getenv("SESSION_SECRET", "").strip()
    if secret:
        return secret
    secret_file = DATA_DIR / ".session_secret"
    try:
        if secret_file.exists():
            saved = secret_file.read_text(encoding="utf-8").strip()
            if saved:
                return saved
        import secrets as _secrets
        secret = _secrets.token_hex(32)
        secret_file.write_text(secret, encoding="utf-8")
        return secret
    except Exception as e:
        # 落盘失败（只读文件系统等）退化为进程内随机：功能可用，重启需重新登录
        print(f"⚠️  会话密钥持久化失败（重启后需重新登录）: {e}")
        import secrets as _secrets
        return _secrets.token_hex(32)

# 确保目录存在
for dir_path in [REPORTS_DIR, DATA_DIR, LOGS_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)


def parse_hhmm(value: str, default: str) -> tuple:
    """解析 HH:MM 字符串为 (hour, minute)，非法值回退默认"""
    for candidate in (value, default):
        try:
            hour_s, minute_s = candidate.strip().split(":")
            hour, minute = int(hour_s), int(minute_s)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except (ValueError, AttributeError):
            continue
    return 8, 0
