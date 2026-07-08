"""
账号与访问控制模块

权限模型（三级）：
- 未登录：只读浏览（页面与查询 API 全部开放）
- 已登录：可发起分析（POST /api/analyze）
- 管理员：可创建/删除账号（注册入口不对外开放，防止项目被陌生人滥用）

密码存储：标准库 pbkdf2_hmac-sha256（26 万次迭代，OWASP 推荐档），
格式 "pbkdf2:迭代次数:盐hex:哈希hex"，迭代数入库保证未来升级强度后旧密码仍可校验。
"""
import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, Request

from app.config import ADMIN_USERNAME, ADMIN_PASSWORD
from app.database import SessionLocal, User

PBKDF2_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    """生成密码哈希，格式 pbkdf2:iterations:salt_hex:hash_hex"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"pbkdf2:{PBKDF2_ITERATIONS}:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码；存储格式损坏时返回 False 而非抛异常"""
    try:
        algo, iterations_s, salt_hex, hash_hex = stored.split(":")
        if algo != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), int(iterations_s),
        )
        # 常数时间比较，避免时序侧信道
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def ensure_admin_user():
    """启动时确保内置管理员存在（幂等：仅用户名不存在时创建）。

    刻意不在已存在时同步密码——管理员可能已在界面改密，
    环境变量默认值不应在每次重启时把密码改回去。
    """
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == ADMIN_USERNAME).first()
        if existing:
            # 历史数据兜底：同名用户若无管理员位则补上（内置账号必须是管理员）
            if not existing.is_admin:
                existing.is_admin = True
                db.commit()
            return
        db.add(User(
            username=ADMIN_USERNAME,
            password_hash=hash_password(ADMIN_PASSWORD),
            is_admin=True,
        ))
        db.commit()
        print(f"👤 已创建内置管理员账号「{ADMIN_USERNAME}」"
              f"（如未通过 ADMIN_PASSWORD 环境变量设置密码，请登录后立即修改）")
    finally:
        db.close()


def get_user_by_id(user_id: int):
    """按 id 查用户，不存在返回 None（会话反查用，删号后立即失效）"""
    if not user_id:
        return None
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id).first()
    finally:
        db.close()


def get_current_user(request: Request):
    """FastAPI 依赖：返回当前登录用户（User 或 None），不强制登录"""
    return getattr(request.state, "user", None)


def require_user(request: Request) -> User:
    """FastAPI 依赖：必须登录，否则 401（前端据此弹登录提示）"""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录后再使用该功能")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    """FastAPI 依赖：必须是管理员，否则 403"""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="仅管理员可执行该操作")
    return user
