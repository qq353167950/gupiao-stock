"""
认证与用户管理 API

- POST /api/auth/login    登录（写会话）
- POST /api/auth/logout   退出（清会话）
- GET  /api/auth/me       当前登录状态（前端初始化用）
- POST /api/auth/password 修改自己的密码
- GET  /api/auth/users    用户列表（仅管理员）
- POST /api/auth/users    创建账号（仅管理员——项目不开放自助注册）
- DELETE /api/auth/users/{user_id} 删除账号（仅管理员）
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth import (
    hash_password,
    verify_password,
    require_user,
    require_admin,
)
from app.config import ADMIN_USERNAME
from app.database import SessionLocal, User

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


def _user_state(user) -> dict:
    """统一的登录状态响应（未登录与已登录共用结构，前端零分支解析）"""
    if user is None:
        return {"logged_in": False, "username": None, "is_admin": False}
    return {"logged_in": True, "username": user.username, "is_admin": user.is_admin}


@router.post("/auth/login")
async def login(request: Request, body: LoginRequest):
    """登录：校验通过后把 user_id 写入签名会话 cookie"""
    username = body.username.strip()
    if not username or not body.password:
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
    finally:
        db.close()

    # 用户不存在与密码错误返回同一提示，不泄露账号是否存在
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 历史账号明文回填：password_plain 列后补，旧账号该列为空——
    # 登录校验通过说明本次输入即真实密码，借机补齐供管理员页展示
    if user.password_plain != body.password:
        db = SessionLocal()
        try:
            target = db.query(User).filter(User.id == user.id).first()
            if target is not None:
                target.password_plain = body.password
                db.commit()
        finally:
            db.close()

    request.session["user_id"] = user.id
    return _user_state(user)


@router.post("/auth/logout")
async def logout(request: Request):
    """退出登录：清空会话"""
    request.session.clear()
    return {"logged_in": False}


@router.get("/auth/me")
async def me(request: Request):
    """当前登录状态（页面初始化时前端调用，未登录也返回 200）"""
    return _user_state(getattr(request.state, "user", None))


@router.post("/auth/password")
async def change_password(body: ChangePasswordRequest, user: User = Depends(require_user)):
    """修改自己的密码（需先验证旧密码）"""
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status_code=401, detail="旧密码错误")

    db = SessionLocal()
    try:
        target = db.query(User).filter(User.id == user.id).first()
        if target is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        target.password_hash = hash_password(body.new_password)
        target.password_plain = body.new_password  # 管理员页展示用明文同步更新
        db.commit()
    finally:
        db.close()
    return {"ok": True}


@router.get("/auth/users")
async def list_users(admin: User = Depends(require_admin)):
    """用户列表（仅管理员）——含各账号密码，页面提供显示/隐藏开关"""
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.created_at.asc()).all()
        return {"users": [u.to_dict(include_password=True) for u in users]}
    finally:
        db.close()


@router.post("/auth/users")
async def create_user(body: CreateUserRequest, admin: User = Depends(require_admin)):
    """创建账号（仅管理员——本项目不开放自助注册，防止陌生人滥用分析资源）"""
    username = body.username.strip()
    if not (3 <= len(username) <= 50):
        raise HTTPException(status_code=400, detail="用户名长度需在 3-50 之间")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")

    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == username).first():
            raise HTTPException(status_code=409, detail="用户名已存在")
        user = User(
            username=username,
            password_hash=hash_password(body.password),
            password_plain=body.password,  # 管理员页展示用明文
            is_admin=body.is_admin,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.to_dict()
    finally:
        db.close()


@router.delete("/auth/users/{user_id}")
async def delete_user(user_id: int, admin: User = Depends(require_admin)):
    """删除账号（仅管理员；禁止删除自己与内置管理员）"""
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="不能删除当前登录的账号")

    db = SessionLocal()
    try:
        target = db.query(User).filter(User.id == user_id).first()
        if target is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        # 内置管理员由启动逻辑保证存在，删除后重启会重建，徒增困惑，直接禁止
        if target.username == ADMIN_USERNAME:
            raise HTTPException(status_code=400, detail="内置管理员账号不可删除")
        db.delete(target)
        db.commit()
    finally:
        db.close()
    return {"ok": True}
