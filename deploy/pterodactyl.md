# Pterodactyl 面板部署指南（panel.adkynet.com）

> AdKyNet 的应用面板基于 **Pterodactyl**（容器化托管，非 root、无 systemd）。
> 本项目已适配该环境：`start.py` 统一处理时区/端口/Cloudflare Tunnel，无需 systemd。

## 前置确认（重要）

在开始前，请在面板确认你的服务器实例满足：

| 项目 | 要求 | 原因 |
|---|---|---|
| 服务器类型 | **Python 应用**（Python egg），非游戏服/Discord bot 专用 egg | 需要跑 uvicorn |
| Python 版本 | 3.10+（egg 的 Docker 镜像标签选 python 3.10/3.11/3.12） | SQLAlchemy 2.0 要求 |
| 内存 | ≥ 2GB（建议 4GB） | 每个分析子进程约 300-500MB |
| 磁盘 | ≥ 15GB | 依赖约 2GB + 报告/缓存增长 |
| 出网 | 允许访问 GitHub、pypi、东财/腾讯/雪球接口 | 数据采集与 skill 更新 |

> ⚠️ **地域提示**：AdKyNet 服务器在欧洲（法国）。A 股数据源（东财 push2/雪球）对境外 IP 常限流。
> 部署后若发现大量 fetcher 超时，在面板变量里配置 `MX_APIKEY`（东财妙想 API）即可走 API 通道。

---

## 一、上传项目

**方式 A：面板文件管理器（推荐）**
1. 本地将项目打包为 zip（**排除** `venv/`、`data/*.db`、`skills/.repo/`、`__pycache__`）
2. 面板 → Files → Upload 上传 zip → 右键 Unarchive 解压到容器根目录（`/home/container`）

**方式 B：Git（若 egg 支持 git clone 变量）**
- 在 egg 的 Git Repo 变量里填你的仓库地址，面板安装时自动拉取

## 二、配置启动命令

Pterodactyl Python egg 通常提供两个关键位置（Startup 页签）：

**1. App py file（主文件）**：
```
start.py
```

**2. 额外依赖安装**：egg 一般有 `Auto update` / `pip install on start` 类变量。
若 egg 支持 requirements 自动安装，填 `requirements.txt`；
若不支持，把 Startup 命令改为（一行）：

```bash
if [ ! -f .deps-installed ]; then python -m pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple || python -m pip install --no-cache-dir -r requirements.txt; touch .deps-installed; fi; python start.py
```

> 说明：首次启动装依赖（约 3-8 分钟），之后凭 `.deps-installed` 标记跳过。
> 更新依赖时删除该文件重启即可。
> Playwright chromium 在 Pterodactyl 容器内通常缺系统库，**不装也不影响核心功能**（仅分享卡截图不可用）。

## 三、配置环境变量

面板 Startup 页签的 Variables（若 egg 变量不够用，也可上传 `.env` 文件到容器根目录，效果相同）：

```ini
# —— 必填 ——
PUBLIC_BASE_URL=https://stock.你的域名.com      # 配好 CF Tunnel 后的对外地址
NOTIFY_CHANNELS=serverchan                      # 推送渠道
SERVERCHAN_SENDKEY=你的SendKey

# —— Cloudflare Tunnel（自定义域名必填，见下文第四节获取）——
CF_TUNNEL_TOKEN=eyJhIjoi...

# —— 境外服务器建议 ——
MX_APIKEY=你的东财妙想Key                        # 数据源限流时的 API 通道
GITHUB_PROXY=                                   # 欧洲服务器直连 GitHub 正常，留空

# —— 可选调优 ——
MAX_CONCURRENT_TASKS=2                          # 按内存调：2GB→1，4GB→2
STOCKS_PER_SECTOR=3
SKILL_UPDATE_INTERVAL_DAYS=3                    # skill 每 3 天检查更新
```

> **端口无需配置**：Pterodactyl 自动注入 `SERVER_PORT`，`start.py` 与 `app/config.py` 已自动适配。

## 四、Cloudflare 自定义域名（Tunnel 方式，推荐）

Pterodactyl 分配的是 `IP:随机端口`，**不适合直接做 CF DNS 记录**（CF 代理只支持标准端口，
且面板 IP/端口可能变化）。正确做法是 **Cloudflare Tunnel**——容器内主动向 CF 建立隧道，
无需公网端口、自动 HTTPS：

### 步骤

1. **域名接入 Cloudflare**：确保你的域名 NS 已托管到 Cloudflare（免费版即可）

2. **创建 Tunnel**：
   - 打开 [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) → Networks → Tunnels
   - Create a tunnel → 选 **Cloudflared** → 命名（如 `stock-analyzer`）→ Save
   - 在 "Install and run a connector" 页面**复制 token**（`eyJhIjoi...` 长字符串，
     即 `cloudflared tunnel run --token <这串>` 中的 token）

3. **配置公共主机名**（同页面 Public Hostname 页签）：
   - Subdomain: `stock`，Domain: `你的域名.com`
   - Service Type: `HTTP`，URL: `localhost:容器端口`
     > 容器端口 = 面板 Network 页签显示的主端口（即 SERVER_PORT 的值）

4. **面板配置变量**：`CF_TUNNEL_TOKEN=复制的token`，重启服务器

5. **验证**：启动日志出现 `✓ Tunnel 已启动`，浏览器访问 `https://stock.你的域名.com`

### 原理

```
用户浏览器 → https://stock.你的域名.com
           → Cloudflare 边缘（自动HTTPS/CDN/隐藏源站）
           → Tunnel 加密隧道（容器内 cloudflared 主动出站连接，无需开放入站端口）
           → localhost:SERVER_PORT (uvicorn)
```

`start.py` 检测到 `CF_TUNNEL_TOKEN` 后会自动下载 cloudflared 二进制（按 CPU 架构）并随主进程启停，无需任何手工操作。

### 备选：不用 Tunnel

若不想用 Tunnel，也可以在 CF DNS 添加 A 记录指向面板 IP（**灰云 DNS only 模式**），
访问 `http://stock.你的域名.com:端口`。缺点：无 HTTPS、端口暴露在 URL 里、面板 IP 变化需手动改。不推荐。

## 五、验证部署

面板 Console 观察启动日志，依次应看到：

```
🌐 启动 Cloudflare Tunnel...        ← 配了 token 才有
🚀 启动 Web 服务: http://0.0.0.0:xxxxx
[内置模式] 检查 Skill...
✅ 定时任务调度器已启动
   - 收盘后分析：每交易日 15:10
   - 早盘推送：  每交易日 08:20
   - 磁盘清理：  每天 03:30
```

然后在面板 Console 里测试推送：

```bash
python -c "from app import notifier; notifier.send('测试', '面板部署成功 ✅')"
```

## 六、常见问题

**启动报 `Address already in use`** → Startup 命令里有残留的旧进程，面板 Kill 后重启。

**分析全部超时** → 欧洲 IP 被数据源限流，配置 `MX_APIKEY`；仍不行则建议换大陆 VPS。

**时间不对/定时任务没在 15:10 触发** → `start.py` 已强制 `TZ=Asia/Shanghai`，若仍不对，
在面板变量加 `TZ=Asia/Shanghai`。

**skill 更新失败 `ls-remote 失败`** → 容器无法直连 GitHub，配置 `GITHUB_PROXY=https://gh-proxy.com/`。

**内存超限被面板 kill** → 调低 `MAX_CONCURRENT_TASKS=1`，或 `DAILY_ANALYSIS_DEPTH=quick`。
