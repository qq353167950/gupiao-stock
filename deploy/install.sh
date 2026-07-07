#!/usr/bin/env bash
# ============================================================
# 股票智能分析平台 · 一键部署脚本（Ubuntu 22.04 / Debian 12）
# 用法：
#   cd /opt && git clone <你的仓库> stock-analyzer-web  # 或 scp 上传
#   cd stock-analyzer-web && bash deploy/install.sh
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "======================================================"
echo "股票智能分析平台 部署脚本"
echo "项目目录: $PROJECT_DIR"
echo "======================================================"

# 1. 系统依赖
echo ""
echo "[1/6] 检查系统依赖..."
if ! command -v python3 >/dev/null; then
    echo "安装 python3..."
    sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
fi
if ! command -v git >/dev/null; then
    echo "安装 git（skill 自动更新需要）..."
    sudo apt-get install -y git
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Python 版本: $PYTHON_VERSION"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
    echo "❌ 需要 Python >= 3.10（SQLAlchemy 2.0 要求），当前 $PYTHON_VERSION"
    exit 1
}

# 2. 虚拟环境
echo ""
echo "[2/6] 创建虚拟环境..."
if [ ! -d venv ]; then
    python3 -m venv venv
fi
source venv/bin/activate

# 3. Python 依赖（清华镜像优先，失败回退官方源）
echo ""
echo "[3/6] 安装 Python 依赖..."
pip install --upgrade pip -q
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple -q \
    || pip install -r requirements.txt -q

# 4. Playwright（可选，仅报告截图分享卡需要；失败不阻塞）
echo ""
echo "[4/6] 安装 Playwright Chromium（可选，失败可忽略）..."
playwright install chromium --with-deps 2>/dev/null || \
    echo "⚠️  Playwright 安装失败（仅影响分享卡截图，核心功能不受影响）"

# 5. 配置文件
echo ""
echo "[5/6] 初始化配置..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "✅ 已生成 .env，请编辑填写推送渠道凭据："
    echo "   nano $PROJECT_DIR/.env"
else
    echo "✓ .env 已存在，跳过"
fi

# 初始化数据库
python3 -c "from app.database import Base, engine; Base.metadata.create_all(bind=engine)"
echo "✓ 数据库初始化完成"

# 6. systemd 服务
echo ""
echo "[6/6] 配置 systemd 服务..."
SERVICE_FILE=/etc/systemd/system/stock-analyzer.service
sed "s|/opt/stock-analyzer-web|$PROJECT_DIR|g" deploy/stock-analyzer.service | sudo tee "$SERVICE_FILE" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable stock-analyzer

echo ""
echo "======================================================"
echo "✅ 部署完成！后续步骤："
echo ""
echo "1. 编辑配置（推送渠道必填）:"
echo "   nano $PROJECT_DIR/.env"
echo ""
echo "2. 启动服务:"
echo "   sudo systemctl start stock-analyzer"
echo ""
echo "3. 查看日志:"
echo "   journalctl -u stock-analyzer -f"
echo ""
echo "4. 访问平台:"
echo "   http://<服务器IP>:8888"
echo ""
echo "5. 测试推送（配好 .env 后）:"
echo "   $PROJECT_DIR/venv/bin/python -c \"from app import notifier; notifier.send('测试', '部署成功 ✅')\""
echo "======================================================"
