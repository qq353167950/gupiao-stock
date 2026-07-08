"""
Skill 管理模块 — UZI-Skill 安装与自动更新

设计：
- 持久化浅克隆仓库到 skills/.repo（保留 .git，支持增量 fetch）
- 版本 = git commit hash，记录在 skills/.skill-version.json
- 更新检查用 `git ls-remote`（单次轻量网络调用），远端 commit 有变化才执行同步
- 同步时保留 skill 目录内的运行时数据（scripts/reports、scripts/.cache）
- 检查频率由 SKILL_UPDATE_INTERVAL_DAYS 控制（默认 3 天），由调度器每日触发
- 大陆网络环境可配 GITHUB_PROXY（如 https://gh-proxy.com/）走镜像代理
"""
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

from app.config import (
    SKILL_MODE,
    AUTO_UPDATE_SKILL,
    BUILTIN_SKILL_PATH,
    EXTERNAL_SKILL_PATH,
    SKILL_REPO_URL,
    SKILL_BRANCH,
    GITHUB_PROXY,
    SKILL_UPDATE_INTERVAL_DAYS,
)

# 持久化仓库与版本文件位置（skills/ 目录下的隐藏成员）
SKILLS_ROOT = BUILTIN_SKILL_PATH.parent
REPO_DIR = SKILLS_ROOT / ".repo"
VERSION_FILE = SKILLS_ROOT / ".skill-version.json"

# 需要同步的 skill 子目录
SKILL_DIRS = ["deep-analysis", "lhb-analyzer", "trap-detector", "investor-panel"]

# 同步时保留的运行时数据（相对 skill 目录的路径）
PRESERVE_PATHS = ["scripts/reports", "scripts/.cache"]

GIT_TIMEOUT_LSREMOTE = 30   # ls-remote 超时（秒）
GIT_TIMEOUT_CLONE = 300     # clone/fetch 超时（秒）


def effective_repo_url(base_url: str = None, proxy: str = None) -> str:
    """计算实际使用的仓库地址（支持 GitHub 镜像代理前缀）。

    代理格式：https://gh-proxy.com/ + 原始 URL，即
    https://gh-proxy.com/https://github.com/wbh604/UZI-Skill.git
    """
    url = base_url if base_url is not None else SKILL_REPO_URL
    prefix = proxy if proxy is not None else GITHUB_PROXY
    if prefix:
        return f"{prefix.rstrip('/')}/{url}"
    return url


def _git_available() -> bool:
    return shutil.which("git") is not None


def _run_git(args: list, cwd: Path = None, timeout: int = GIT_TIMEOUT_CLONE) -> Tuple[int, str, str]:
    """执行 git 命令，返回 (returncode, stdout, stderr)"""
    proc = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# ─── 版本记录 ───

def read_version() -> dict:
    """读取版本文件，不存在或损坏返回空 dict"""
    if not VERSION_FILE.exists():
        return {}
    try:
        return json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_version(commit: str, updated: bool):
    """写版本文件。updated=True 表示本次实际同步了文件（更新 updated_at）"""
    data = read_version()
    data["commit"] = commit
    data["checked_at"] = datetime.now().isoformat()
    if updated:
        data["updated_at"] = datetime.now().isoformat()
    data["source"] = effective_repo_url()
    VERSION_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def should_check_now(version_data: dict, interval_days: int, now: datetime = None) -> bool:
    """根据上次检查时间和间隔判断是否需要执行更新检查（纯函数，便于测试）"""
    now = now or datetime.now()
    checked_at_s = version_data.get("checked_at")
    if not checked_at_s:
        return True
    try:
        checked_at = datetime.fromisoformat(checked_at_s)
    except ValueError:
        return True
    return (now - checked_at) >= timedelta(days=interval_days)


# ─── 远端与本地 commit ───

def get_remote_commit() -> Optional[str]:
    """`git ls-remote` 获取远端分支最新 commit（轻量，无需本地仓库）"""
    try:
        code, out, err = _run_git(
            ["ls-remote", effective_repo_url(), SKILL_BRANCH],
            timeout=GIT_TIMEOUT_LSREMOTE,
        )
        if code == 0 and out.strip():
            return out.split()[0]
        print(f"   ⚠️  ls-remote 失败: {err.strip()[:200]}")
    except subprocess.TimeoutExpired:
        print(f"   ⚠️  ls-remote 超时（{GIT_TIMEOUT_LSREMOTE}s）")
    except Exception as e:
        print(f"   ⚠️  ls-remote 异常: {e}")
    return None


def _refresh_repo() -> Optional[str]:
    """确保 skills/.repo 为远端最新状态，返回其 commit hash。

    - 仓库存在 → fetch --depth 1 + reset --hard（浅仓库增量更新）
    - 不存在或增量失败 → 删除后全新浅克隆
    """
    url = effective_repo_url()

    def _current_commit() -> Optional[str]:
        code, out, _ = _run_git(["rev-parse", "HEAD"], cwd=REPO_DIR, timeout=15)
        return out.strip() if code == 0 else None

    # 尝试增量更新
    if (REPO_DIR / ".git").exists():
        code1, _, err1 = _run_git(
            ["fetch", "--depth", "1", "origin", SKILL_BRANCH], cwd=REPO_DIR
        )
        if code1 == 0:
            code2, _, err2 = _run_git(
                ["reset", "--hard", f"origin/{SKILL_BRANCH}"], cwd=REPO_DIR, timeout=60
            )
            if code2 == 0:
                return _current_commit()
            print(f"   ⚠️  git reset 失败: {err2.strip()[:200]}")
        else:
            print(f"   ⚠️  git fetch 失败: {err1.strip()[:200]}")
        # 增量失败 → 删除重来
        shutil.rmtree(REPO_DIR, ignore_errors=True)

    # 全新浅克隆
    print(f"   克隆仓库: {url}")
    REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
    code, _, err = _run_git([
        "clone", "--depth", "1",
        "--branch", SKILL_BRANCH, "--single-branch",
        url, str(REPO_DIR),
    ])
    if code != 0:
        print(f"   ❌ git clone 失败: {err.strip()[:300]}")
        return None
    return _current_commit()


# ─── 目录同步 ───

def _sync_one_skill(src: Path, dst: Path, preserve_paths=None):
    """用 src 替换 dst，但保留 dst 中 preserve_paths 指定的运行时数据。

    步骤：先把 src 完整复制到 dst 同级临时目录（失败则旧目录原样保留）→
    暂存保留目录 → 删除 dst → 原子重命名新目录为 dst → 恢复保留目录。
    暂存与临时目录都建在 dst 同级（同文件系统，move 为原子重命名）。
    任一环节失败时尽力把暂存数据搬回，实在搬不回则保留暂存目录并打印路径，
    绝不静默删除运行时数据（报告与缓存）。
    """
    preserve_paths = PRESERVE_PATHS if preserve_paths is None else preserve_paths
    dst.parent.mkdir(parents=True, exist_ok=True)

    # 第一步：先复制新版本到临时目录，复制失败时旧 skill 完好无损
    incoming = Path(tempfile.mkdtemp(prefix=f".incoming-{dst.name}-", dir=dst.parent)) / dst.name
    try:
        shutil.copytree(src, incoming)
    except Exception:
        shutil.rmtree(incoming.parent, ignore_errors=True)
        raise

    staging = None
    preserved: list = []
    restored: list = []
    try:
        if dst.exists():
            # 暂存运行时数据
            existing = [rel for rel in preserve_paths if (dst / rel).exists()]
            if existing:
                staging = Path(tempfile.mkdtemp(prefix=f".preserve-{dst.name}-", dir=dst.parent))
                for rel in existing:
                    target = staging / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dst / rel), str(target))
                    preserved.append(rel)
            shutil.rmtree(dst)

        # 原子重命名（同文件系统），比逐文件复制的失败窗口小得多
        incoming.rename(dst)

        # 恢复运行时数据（新版本若自带同名空目录，先让位）
        for rel in preserved:
            restore_to = dst / rel
            if restore_to.exists():
                shutil.rmtree(restore_to)
            restore_to.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging / rel), str(restore_to))
            restored.append(rel)
    except Exception:
        # 尽力把已暂存但未恢复的数据搬回（dst 此时可能是旧目录残骸或新目录）
        pending = [rel for rel in preserved if rel not in restored]
        if staging and pending and dst.exists():
            for rel in pending:
                try:
                    restore_to = dst / rel
                    if not restore_to.exists():
                        restore_to.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(staging / rel), str(restore_to))
                        restored.append(rel)
                except Exception:
                    pass
        raise
    finally:
        shutil.rmtree(incoming.parent, ignore_errors=True)
        if staging and staging.exists():
            leftover = [rel for rel in preserved if rel not in restored]
            if leftover:
                # 还有数据没搬回：绝不删除，保留暂存目录供人工恢复
                print(f"   ⚠️  运行时数据未能全部恢复，已保留在: {staging}（含 {leftover}）")
            else:
                shutil.rmtree(staging, ignore_errors=True)


def _sync_all_skills() -> int:
    """从 .repo 同步全部 skill 目录，返回成功同步数量"""
    repo_skills = REPO_DIR / "skills"
    if not repo_skills.exists():
        print(f"   ❌ 仓库中未找到 skills 目录")
        return 0

    synced = 0
    for name in SKILL_DIRS:
        src = repo_skills / name
        if not src.exists():
            print(f"   ⚠️  仓库中缺少 {name}，跳过")
            continue
        _sync_one_skill(src, SKILLS_ROOT / name)
        print(f"   ✓ {name}")
        synced += 1
    return synced


# ─── 对外接口 ───

def check_skill_exists(skill_path: Path) -> bool:
    """检查skill是否存在"""
    return skill_path.exists() and (skill_path / "run.py").exists()


def install_or_update_skills(force: bool = False) -> bool:
    """安装或更新 skills 到远端最新版本。

    Args:
        force: True 时跳过 ls-remote 短路比对，强制刷新仓库并同步

    Returns:
        本次是否实际同步了文件
    """
    if not _git_available():
        print("   ❌ 未检测到 git，无法安装/更新 skill（请安装 git）")
        return False

    local_commit = read_version().get("commit")

    # 轻量短路：远端 commit 与本地一致且 skill 完整 → 无需任何重操作
    if not force:
        remote_commit = get_remote_commit()
        if remote_commit is None:
            print("   ⚠️  无法获取远端版本，本次跳过（保持现有 skill）")
            return False
        if remote_commit == local_commit and check_skill_exists(BUILTIN_SKILL_PATH):
            write_version(remote_commit, updated=False)  # 刷新 checked_at
            print(f"   ✓ 已是最新版本（{remote_commit[:8]}）")
            return False
        print(f"   发现新版本: {(local_commit or '无')[:8]} → {remote_commit[:8]}")

    # 刷新持久化仓库并同步
    new_commit = _refresh_repo()
    if not new_commit:
        return False

    synced = _sync_all_skills()
    if synced == 0:
        return False

    write_version(new_commit, updated=True)
    print(f"   ✅ Skills 已更新至 {new_commit[:8]}（同步 {synced} 个 skill，报告与缓存已保留）")
    return True


def _has_active_analysis() -> bool:
    """是否有正在运行/排队的分析任务（更新 skill 会删除其工作目录，必须互斥）"""
    try:
        from app.database import SessionLocal, AnalysisTask
        db = SessionLocal()
        try:
            n = db.query(AnalysisTask).filter(
                AnalysisTask.status.in_(["running", "pending"])
            ).count()
            return n > 0
        finally:
            db.close()
    except Exception:
        # 查询失败时保守处理：视为有任务在跑，跳过更新
        return True


def check_and_update_skill(respect_interval: bool = True) -> bool:
    """更新检查入口（启动时与定时任务共用）。

    Args:
        respect_interval: True 时按 SKILL_UPDATE_INTERVAL_DAYS 间隔限频

    Returns:
        本次是否实际更新了文件
    """
    if SKILL_MODE != "builtin" or not AUTO_UPDATE_SKILL:
        return False

    version_data = read_version()
    if respect_interval and not should_check_now(version_data, SKILL_UPDATE_INTERVAL_DAYS):
        checked_at = version_data.get("checked_at", "")[:16]
        print(f"   ✓ 距上次检查（{checked_at}）未满 {SKILL_UPDATE_INTERVAL_DAYS} 天，跳过")
        return False

    # 互斥保护：有分析任务在跑时更新会删除其工作目录导致任务崩溃
    if _has_active_analysis():
        print("   ⚠️  存在运行中/排队中的分析任务，本次跳过 skill 更新")
        return False

    print(f"\n[Skill 更新] 检查 UZI-Skill 更新...")
    return install_or_update_skills(force=False)


def ensure_skills_ready():
    """应用启动入口：保证 skill 可用，并按间隔执行更新检查"""
    if SKILL_MODE == "external":
        print(f"\n[外部模式] 使用外部 Skill 路径")
        print(f"  路径: {EXTERNAL_SKILL_PATH}")
        if not check_skill_exists(EXTERNAL_SKILL_PATH):
            print(f"  ⚠️  外部路径无效（缺少 run.py），分析功能将不可用")
        return

    print(f"\n[内置模式] 检查 Skill...")
    if not check_skill_exists(BUILTIN_SKILL_PATH):
        print("  Skill 未安装，开始安装...")
        install_or_update_skills(force=True)
    else:
        version = read_version()
        commit = (version.get("commit") or "未知")[:8]
        print(f"  ✓ Skill 已就绪（版本 {commit}）")
        check_and_update_skill(respect_interval=True)


if __name__ == "__main__":
    import sys

    command = sys.argv[1] if len(sys.argv) > 1 else ""

    if command == "install":
        install_or_update_skills(force=True)
    elif command == "update":
        # 手动更新：不受间隔限制，但仍走 ls-remote 短路
        install_or_update_skills(force=False)
    elif command == "check":
        if check_skill_exists(BUILTIN_SKILL_PATH):
            v = read_version()
            print(f"✓ Skill 已安装")
            print(f"  路径: {BUILTIN_SKILL_PATH}")
            print(f"  版本: {(v.get('commit') or '未知')[:12]}")
            print(f"  上次检查: {v.get('checked_at', '从未')}")
            print(f"  上次更新: {v.get('updated_at', '从未')}")
            remote = get_remote_commit()
            if remote:
                status = "已是最新" if remote == v.get("commit") else f"有新版本 {remote[:8]}"
                print(f"  远端状态: {status}")
        else:
            print(f"✗ Skill 未安装")
    else:
        print("用法: python -m app.skill_manager [install|update|check]")
        print("  install  强制重新安装（刷新到远端最新）")
        print("  update   检查并更新（有新版本才动文件）")
        print("  check    查看本地与远端版本状态")
