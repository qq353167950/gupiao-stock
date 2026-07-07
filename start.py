#!/usr/bin/env python3
"""
容器面板通用启动器（Pterodactyl / Docker / 任意 PaaS）

职责：
1. 设置时区为 Asia/Shanghai（A股调度依赖北京时间，海外容器必须校正）
2. 可选启动 Cloudflare Tunnel（配置 CF_TUNNEL_TOKEN 即启用，实现自定义域名 HTTPS 访问）
3. 启动 FastAPI 应用（端口自动适配 PORT / SERVER_PORT）

Pterodactyl 用法：Python egg 的 App py file 填 start.py 即可。
本地用法：python start.py
"""
import os
import platform
import stat
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))

# ─── 1. 时区校正（必须在导入 app 之前）───
# APScheduler 与 datetime.now() 均使用本地时区；海外容器默认 UTC 会导致
# 15:10 收盘分析实际在北京时间 23:10 才跑。强制北京时间保证调度语义正确。
os.environ.setdefault("TZ", "Asia/Shanghai")
if hasattr(time, "tzset"):  # 仅 Unix；Windows 本地调试跳过
    time.tzset()

os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _cloudflared_download_url() -> str:
    """按 CPU 架构选择 cloudflared 二进制下载地址（支持 GITHUB_PROXY 镜像前缀）"""
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
    url = (
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        f"cloudflared-linux-{arch}"
    )
    proxy = os.environ.get("GITHUB_PROXY", "").strip()
    if proxy:
        url = f"{proxy.rstrip('/')}/{url}"
    return url


def _ensure_cloudflared() -> str:
    """定位 cloudflared 二进制：优先系统预装（Docker 镜像内置），否则下载到项目目录"""
    import shutil as _shutil
    system_binary = _shutil.which("cloudflared")
    if system_binary:
        return system_binary

    binary = BASE_DIR / "cloudflared"
    if not binary.exists():
        url = _cloudflared_download_url()
        print(f"📥 下载 cloudflared: {url}")
        import urllib.request
        urllib.request.urlretrieve(url, binary)  # noqa: S310 — 固定官方地址
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return str(binary)


def start_cloudflare_tunnel() -> "subprocess.Popen | None":
    """配置了 CF_TUNNEL_TOKEN 则启动命名隧道（后台常驻，随主进程退出）"""
    token = os.environ.get("CF_TUNNEL_TOKEN", "").strip()
    if not token:
        print("ℹ️  未配置 CF_TUNNEL_TOKEN，跳过 Cloudflare Tunnel（仅面板分配的 IP:端口 可访问）")
        return None

    try:
        binary = _ensure_cloudflared()
        print("🌐 启动 Cloudflare Tunnel...")
        proc = subprocess.Popen(
            [binary, "tunnel", "--no-autoupdate", "run", "--token", token],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        print("   ✓ Tunnel 已启动（域名解析以 CF Zero Trust 面板配置为准）")
        return proc
    except Exception as e:
        print(f"   ⚠️  Tunnel 启动失败（不影响主服务）: {e}")
        return None


def main():
    tunnel_proc = start_cloudflare_tunnel()

    # 端口优先级与 app.config 一致：PORT > SERVER_PORT（Pterodactyl 注入）> 8888
    port = int(os.environ.get("PORT") or os.environ.get("SERVER_PORT") or 8888)
    host = os.environ.get("HOST", "0.0.0.0")

    print(f"🚀 启动 Web 服务: http://{host}:{port}")
    try:
        import uvicorn
        uvicorn.run("app.main:app", host=host, port=port, log_level="info")
    finally:
        if tunnel_proc:
            tunnel_proc.terminate()


if __name__ == "__main__":
    main()
