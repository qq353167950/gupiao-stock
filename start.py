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
# app 内业务时间已统一走 config.now_cn()（显式北京时间，不依赖本进程时区），
# 此处强制 TZ 主要为 skill 子进程（deep-analysis 等用 datetime.now()）兜底：
# 海外容器默认 UTC 会导致其报告日期/缓存键错位。
# 注意必须强制覆盖而非 setdefault：Pterodactyl 等面板会注入宿主机 TZ（如
# America/New_York），setdefault 会被其抢占，导致子进程仍跑在外国时区。
os.environ["TZ"] = "Asia/Shanghai"
if hasattr(time, "tzset"):  # 仅 Unix；Windows 本地调试跳过
    time.tzset()

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# 当前进程的 stdout/stderr 立即切 UTF-8（上面的环境变量只影响子进程）：
# Windows GBK 控制台下打印 emoji/特殊符号会直接 UnicodeEncodeError 崩溃
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


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
        # 先下到临时文件再原子重命名：避免下载中断留下损坏的二进制被下次启动复用；
        # 带超时避免网络挂起导致 Web 服务永远起不来
        tmp = binary.with_suffix(".tmp")
        try:
            with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as f:  # noqa: S310 — 固定官方地址
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
            tmp.replace(binary)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
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


def _ensure_playwright_browser():
    """确保 Playwright Chromium 可用（分享卡/战报截图依赖）。

    Pterodactyl 等无 root 面板环境首次启动时自动下载 chromium 到用户缓存目录，
    已安装则秒级跳过。失败仅打印警告，不影响主服务（截图功能会自动跳过）。
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # executable_path 存在即已安装，避免每次启动都跑 install
            if Path(p.chromium.executable_path).exists():
                return
    except ImportError:
        return  # 未安装 playwright 包，无需处理
    except Exception:
        pass  # 检测失败则尝试安装

    print("📥 首次安装 Playwright Chromium（分享卡/战报截图依赖，约 1-2 分钟）...")
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True, timeout=600,
        )
        print("   ✓ Chromium 安装完成")
    except Exception as e:
        print(f"   ⚠️  Chromium 安装失败（不影响主服务，仅分享卡功能跳过）: {e}")


def main():
    _ensure_playwright_browser()
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
