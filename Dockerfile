# ============================================================
# 股票智能分析平台 · Docker 镜像
# 构建：docker compose build
# 启动：docker compose up -d
# ============================================================
FROM python:3.12-slim-bookworm

# 时区：A股调度依赖北京时间
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1

# 系统依赖：
# - git: UZI-Skill 自动更新
# - curl/ca-certificates: 数据抓取与 cloudflared 下载
# - tzdata: 时区数据
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 内置 cloudflared（自定义域名 Tunnel 用；按构建平台选架构）
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSL -o /usr/local/bin/cloudflared \
       "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}" \
    && chmod +x /usr/local/bin/cloudflared

WORKDIR /app

# 先装依赖（利用 Docker 层缓存：requirements 不变时重构建秒级完成）
COPY requirements.txt .
RUN pip install -r requirements.txt \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
    || pip install -r requirements.txt

# Playwright 浏览器（分享卡/战报截图与数据抓取降级路径依赖，缺失会导致相关功能跳过）
RUN playwright install --with-deps chromium

# 复制项目代码
COPY . .

# 构建期冒烟：验证应用可导入（表结构由 app/database.py 在运行时自动创建，
# data/ 为 volume 挂载，构建期建库无意义）
RUN python -c "import app.config, app.notifier, app.skill_manager; print('build smoke ok')"

EXPOSE 8888

# 健康检查：探测 /health 接口（端口优先级与应用一致：PORT > SERVER_PORT > 8888）
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-${SERVER_PORT:-8888}}/health" || exit 1

CMD ["python", "start.py"]
