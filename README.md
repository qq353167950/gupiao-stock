# 股票分析推荐系统

基于 UZI-Skill 深度分析和多维度评估的 A 股智能推荐系统。每日自动选股 → 批量深度分析 → 生成报告 → 多渠道推送。

## 功能特性

- **深度分析**：19 维度全面分析 + 65 位投资大佬评审（UZI-Skill deep-analysis）
- **风险检测**：真实 web 搜索 8 信号扫描（fetch_trap_signals），LLM 兜底
- **龙虎榜分析**：akshare 真实龙虎榜数据 + 游资席位库匹配（fetch_lhb）
- **综合评分**：0-100 分评分系统，四级风险评级
- **自动推荐**：每交易日收盘后自动分析，开盘前生成推荐
- **多渠道推送**：Server酱 / PushPlus / 钉钉 / 企业微信 / Telegram / 邮件
- **Web 界面**：实时查看分析进度和推荐结果

## 系统架构

```
├── app/                      # 应用核心代码
│   ├── main.py              # FastAPI 主应用
│   ├── config.py            # 统一配置（.env 加载 + 环境变量覆盖）
│   ├── database.py          # 数据库模型
│   ├── task_manager.py      # 分析任务管理（含并发限流）
│   ├── enhanced_analyzer.py # 增强分析引擎（trap→deep→lhb 三阶段）
│   ├── notifier.py          # 多渠道推送
│   ├── scheduler.py         # 定时任务（分析/推送/清理）
│   ├── trading_calendar.py  # 交易日历（akshare 官方数据+缓存）
│   ├── llm_client.py        # LLM 客户端（可选）
│   └── skill_manager.py     # Skill 安装与自动更新
├── skills/                  # UZI-Skill 分析工具
├── deploy/                  # 部署物料（systemd + 一键脚本）
├── tests/                   # 单元测试
├── templates/ static/       # Web 前端
├── data/ logs/ reports/     # 运行时数据
```

## 每日自动流程

```
15:10 收盘后（交易日）
  → 腾讯行情选股（每大板块 TOP N，默认 3 只 × 6 板块 = 18 只）
  → 并发批量分析（默认并发 2，standard 深度约 25 分钟/只）
  → 全部完成后生成次日推荐 + 立即推送分析摘要
08:20 开盘前（交易日）
  → 推送当日推荐至所有已配置渠道
03:30 每天
  → 清理过期报告/缓存/日志/历史任务记录
```

## 快速开始（服务器部署）

> 📖 **零基础手把手教程**（每步点什么/填什么 + Cloudflare 自定义域名全流程）：
> **[deploy/部署指南.md](deploy/部署指南.md)** ← 推荐从这里开始

### 方式一：Docker 一键部署（推荐）

```bash
cd /opt/stock-analyzer-web
cp .env.example .env && nano .env   # 填 PUBLIC_BASE_URL + 推送渠道，共 3 行
docker compose up -d --build        # 一键构建并启动
docker compose logs -f              # 观察启动日志
```

镜像内置：北京时区、git（skill 自动更新）、cloudflared（自定义域名 Tunnel）、
健康检查；数据库/日志/报告/skill 全部挂载宿主机，容器重建不丢数据。

### 方式二：Pterodactyl 容器面板（panel.adkynet.com 等）

见 [deploy/部署指南.md](deploy/部署指南.md) 方式 B 或 [deploy/pterodactyl.md](deploy/pterodactyl.md)。
启动文件统一为 `start.py`（自动处理时区/端口/Tunnel）。

### 方式三：裸机 systemd（Ubuntu/Debian）

```bash
cd /opt
git clone <你的仓库地址> stock-analyzer-web   # 或 scp 上传
cd stock-analyzer-web
bash deploy/install.sh
```

脚本自动完成：系统依赖检查 → venv → pip 依赖（清华镜像 fallback）→ 数据库初始化 → systemd 服务注册。

### 配置（必做）

```bash
cp .env.example .env   # install.sh 已自动生成
nano .env
```

最少只需配两项：

```ini
# 对外访问地址（推送消息里的报告链接）
PUBLIC_BASE_URL=http://你的服务器IP:8888

# 推送渠道（至少一个；SendKey 在 https://sct.ftqq.com 免费申请）
NOTIFY_CHANNELS=serverchan
SERVERCHAN_SENDKEY=你的SendKey
```

LLM API 可选（不配置不影响核心功能，仅 trap-detector 失去 LLM 降级路径）：

```bash
cp .env.llm.example .env.llm && nano .env.llm
```

### 启动

```bash
sudo systemctl start stock-analyzer
journalctl -u stock-analyzer -f        # 查看日志
```

访问：`http://服务器IP:8888`

### 验证部署

```bash
# 1. 健康检查
curl http://localhost:8888/health

# 2. 测试推送渠道
venv/bin/python -c "from app import notifier; notifier.send('测试', '部署成功 ✅')"

# 3. 单元测试
venv/bin/python tests/test_core.py

# 4. 手动触发一次快速分析（约 7 分钟/只）
venv/bin/python auto_analyze_and_recommend.py quick morning
```

## 环境要求

- **系统**：Linux（推荐 Ubuntu 22.04+）；服务器建议中国大陆地域（数据源为东财/雪球/腾讯，境外 IP 常被限流，境外部署需配 `MX_APIKEY`）
- **Python**：3.10+
- **配置**：最低 2核4G / 40GB SSD（并发 2）；推荐 4核8G / 60GB SSD（并发 3-4）

## 主要配置项

全部配置见 `.env.example`，常用项：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `MAX_CONCURRENT_TASKS` | 2 | 并发分析数（每任务约占 300-500MB 内存）|
| `STOCKS_PER_SECTOR` | 3 | 每大板块每日选股数（×6 板块）|
| `DAILY_ANALYSIS_DEPTH` | standard | 每日定时分析深度 quick/standard/deep |
| `AFTER_MARKET_ANALYSIS_TIME` | 15:10 | 收盘分析时间 |
| `MORNING_PUSH_TIME` | 08:20 | 早盘推送时间 |
| `NOTIFY_CHANNELS` | 空 | 推送渠道，逗号分隔可多选 |
| `SKILL_UPDATE_INTERVAL_DAYS` | 3 | UZI-Skill 更新检查间隔（天）|
| `GITHUB_PROXY` | 空 | GitHub 镜像代理（大陆网络配 `https://gh-proxy.com/`）|
| `CF_TUNNEL_TOKEN` | 空 | Cloudflare Tunnel token（自定义域名用）|
| `REPORT_RETENTION_DAYS` | 7 | 报告保留天数 |

## UZI-Skill 自动更新

skill 来自官方仓库 [wbh604/UZI-Skill](https://github.com/wbh604/UZI-Skill)，系统自动保持最新：

- **检查时机**：应用启动时 + 每天清理任务（03:30）触发，按 `SKILL_UPDATE_INTERVAL_DAYS`（默认 3 天）限频
- **轻量比对**：`git ls-remote` 获取远端 commit（一次网络调用），与本地版本一致则直接跳过
- **增量更新**：有新 commit 才 fetch 同步，`skills/deep-analysis/scripts/` 下的**报告与缓存自动保留**
- **版本可查**：`skills/.skill-version.json` 记录当前 commit 与检查/更新时间

手动操作：

```bash
python -m app.skill_manager check    # 查看本地/远端版本状态
python -m app.skill_manager update   # 立即检查更新（有新版本才动文件）
python -m app.skill_manager install  # 强制重装到远端最新
```

## API 接口

| 接口 | 说明 |
|---|---|
| `POST /api/analyze` | 发起分析 `{"ticker": "600519", "depth": "standard"}` |
| `GET /api/task/{task_id}` | 查询任务状态 |
| `GET /api/recommendations/today` | 今日推荐（分板块）|
| `GET /api/recommendations/history` | 历史推荐 |
| `GET /api/history` | 分析历史 |
| `GET /health` | 健康检查 |

## 常见问题

**推送没收到？**
- 检查 `.env` 中 `NOTIFY_CHANNELS` 与对应凭据
- 手动测试：`venv/bin/python -c "from app import notifier; notifier.send('测试','ok')"`
- 查看日志中 `推送 [渠道]` 行的失败原因

**分析任务失败/超时？**
- `journalctl -u stock-analyzer -f` 查看具体报错
- 数据源限流常见于境外服务器 → 换大陆服务器或配 `MX_APIKEY`
- 深度分析超时可调 `ANALYSIS_TIMEOUT_STANDARD`（默认 1800 秒）

**磁盘增长过快？**
- 调小 `REPORT_RETENTION_DAYS` / `CACHE_RETENTION_DAYS`
- 清理任务每天 `CLEANUP_TIME`（默认 03:30）自动执行

**修改了数据库结构？**
```bash
venv/bin/python -c "from app.database import Base, engine; Base.metadata.create_all(bind=engine)"
```

## 技术栈

- **后端**：FastAPI + SQLAlchemy + APScheduler
- **数据库**：SQLite
- **前端**：HTML + Jinja2 模板 + WebSocket
- **分析引擎**：UZI-Skill（deep-analysis / fetch_trap_signals / fetch_lhb）
- **数据源**：akshare / 腾讯财经 / 东方财富 / 雪球
- **LLM**：OpenAI 兼容 API（可选）

## 免责声明

本系统生成的分析和推荐仅供学习和研究使用，不构成任何投资建议。股市有风险，投资需谨慎。使用本系统进行投资决策造成的任何损失，开发者不承担责任。

## 许可证

MIT License
