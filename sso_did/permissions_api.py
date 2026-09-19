import os
import sys
from fastapi import APIRouter, Depends

# ================= 路径配置及import内部功能 ===============
PROJECT_ROOT = os.getenv("WORK_PATH", "")
if __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sso_did.auth import TokenData, require_permission
from sso_did.rbac_models import Response, PermissionCreate, PermissionQuery
from sso_did.rbac_service import PermissionService

router = APIRouter(prefix="/api/v1/permissions", tags=["Permissions"])
svc = PermissionService()


@router.get("", response_model=Response)
async def list_permissions(
    status: str | None = None,
    _: TokenData = Depends(require_permission("system:permission:list")),
):
    """查询所有权限（全量，供角色编辑勾选用）。"""
    data = svc.list_all(status)
    return Response.ok(data=data)


@router.get("/page", response_model=Response)
async def list_permissions_page(
    query: PermissionQuery = Depends(),
    _: TokenData = Depends(require_permission("system:permission:list")),
):
    """分页查询权限。"""
    data = svc.list_page(
        page=query.pageNo, size=query.pageSize,
        sort_field=query.sortField or "create_time", sort_asc=query.sortAsc,
        status=query.status, name=query.name, code=query.code, type=query.type,
    )
    return Response.ok(data=data)


@router.get("/{permission_id}", response_model=Response)
async def get_permission(
    permission_id: int,
    _: TokenData = Depends(require_permission("system:permission:list")),
):
    """获取权限详情。"""
    data = svc.get_by_id(permission_id)
    if not data:
        return Response.fail(code=404, message="Permission not found")
    return Response.ok(data=data)


@router.post("", response_model=Response)
async def create_permission(
    body: PermissionCreate,
    _: TokenData = Depends(require_permission("system:permission:save")),
):
    """创建权限。"""
    permission_id = svc.create(body)
    return Response.ok(data=permission_id)


@router.put("/{permission_id}", response_model=Response)
async def update_permission(
    permission_id: int,
    body: PermissionCreate,
    _: TokenData = Depends(require_permission("system:permission:save")),
):
    """更新权限。"""
    svc.update(permission_id, body)
    return Response.ok(data=permission_id)


@router.delete("/{permission_id}", response_model=Response)
async def delete_permission(
    permission_id: int,
    _: TokenData = Depends(require_permission("system:permission:delete")),
):
    """删除权限（软删，并清理角色-权限关联）。"""
    svc.delete(permission_id)
    return Response.ok(data=permission_id)
