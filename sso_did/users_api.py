import os
import sys
from fastapi import APIRouter, Depends

# ================= 路径配置及import内部功能 ===============
PROJECT_ROOT = os.getenv("WORK_PATH", "")
if __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sso_did.auth import TokenData, get_current_user, require_permission
from sso_did.rbac_models import Response, UserCreate, UserQuery
from sso_did.rbac_service import UserService

router = APIRouter(prefix="/api/v1/users", tags=["Users"])
svc = UserService()


@router.get("/current-user", response_model=Response)
async def get_current_user_info(current_user: TokenData = Depends(get_current_user)):
    """获取当前登录用户的完整画像（登录即可访问）。

    返回邮箱/部门/工号/角色/权限码等全部字段，而非仅 uid/username。
    roles/permissions 优先取 JWT（签发时已查库），库表补其余字段。
    """
    data = svc.get_login_user(current_user.uid, current_user.roles, current_user.permissions)
    return Response.ok(data=data)


@router.get("/page", response_model=Response)
async def list_users(
    query: UserQuery = Depends(),
    _: TokenData = Depends(require_permission("system:user:list")),
):
    """分页查询用户。"""
    data = svc.list_page(
        page=query.pageNo, size=query.pageSize,
        sort_field=query.sortField or "create_time", sort_asc=query.sortAsc,
        uid=query.uid, display_name=query.displayName,
        email=query.email, department=query.department, status=query.status,
    )
    return Response.ok(data=data)


@router.get("/{user_id}", response_model=Response)
async def get_user(
    user_id: int,
    _: TokenData = Depends(require_permission("system:user:list")),
):
    """获取用户详情。"""
    data = svc.get_by_id(user_id)
    if not data:
        return Response.fail(code=404, message="User not found")
    return Response.ok(data=data)


@router.post("", response_model=Response)
async def create_user(
    body: UserCreate,
    _: TokenData = Depends(require_permission("system:user:save")),
):
    """创建用户。"""
    user_id = svc.create(body)
    return Response.ok(data=user_id)


@router.put("/{user_id}", response_model=Response)
async def update_user(
    user_id: int,
    body: UserCreate,
    _: TokenData = Depends(require_permission("system:user:save")),
):
    """更新用户。"""
    svc.update(user_id, body)
    return Response.ok(data=user_id)


@router.delete("/{user_id}", response_model=Response)
async def delete_user(
    user_id: int,
    _: TokenData = Depends(require_permission("system:user:delete")),
):
    """删除用户（软删）。"""
    svc.delete(user_id)
    return Response.ok(data=user_id)


@router.get("/{user_id}/roles", response_model=Response)
async def get_user_roles(
    user_id: int,
    _: TokenData = Depends(require_permission("system:user:list")),
):
    """获取用户角色。"""
    user = svc.get_by_id(user_id)
    if not user:
        return Response.fail(code=404, message="User not found")
    data = svc.get_roles(user.uid)
    return Response.ok(data=data)
