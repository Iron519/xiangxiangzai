import os
import sys
from fastapi import APIRouter, Depends

# ================= 路径配置及import内部功能 ===============
PROJECT_ROOT = os.getenv("WORK_PATH", "")
if __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sso_did.auth import TokenData, require_permission
from sso_did.rbac_models import Response, RoleCreate, RoleQuery, RolePermissionAssign
from sso_did.rbac_service import RoleService

router = APIRouter(prefix="/api/v1/roles", tags=["Roles"])
svc = RoleService()

@router.get("", response_model=Response)
async def list_roles(
    status: str | None = None,
    _: TokenData = Depends(require_permission("system:role:list")),
):
    """查询所有角色。"""
    data = svc.list_all(status)
    return Response.ok(data=data)


@router.get("/page", response_model=Response)
async def list_roles_page(
    query: RoleQuery = Depends(),
    _: TokenData = Depends(require_permission("system:role:list")),
):
    """分页查询角色。"""
    data = svc.list_page(
        page=query.pageNo, size=query.pageSize,
        sort_field=query.sortField or "create_time", sort_asc=query.sortAsc,
        status=query.status, name=query.name,
    )
    return Response.ok(data=data)


@router.get("/{role_id}", response_model=Response)
async def get_role(
    role_id: int,
    _: TokenData = Depends(require_permission("system:role:list")),
):
    """获取角色详情。"""
    data = svc.get_by_id(role_id)
    if not data:
        return Response.fail(code=404, message="Role not found")
    return Response.ok(data=data)


@router.post("", response_model=Response)
async def create_role(
    body: RoleCreate,
    _: TokenData = Depends(require_permission("system:role:save")),
):
    """创建角色。"""
    role_id = svc.create(body)
    return Response.ok(data=role_id)


@router.put("/{role_id}", response_model=Response)
async def update_role(
    role_id: int,
    body: RoleCreate,
    _: TokenData = Depends(require_permission("system:role:save")),
):
    """更新角色。"""
    svc.update(role_id, body)
    return Response.ok(data=role_id)


@router.delete("/{role_id}", response_model=Response)
async def delete_role(
    role_id: int,
    _: TokenData = Depends(require_permission("system:role:delete")),
):
    """删除角色（软删，被使用时拒绝）。"""
    svc.delete(role_id)
    return Response.ok(data=role_id)


@router.get("/{role_id}/permissions", response_model=Response)
async def get_role_permissions(
    role_id: int,
    _: TokenData = Depends(require_permission("system:role:list")),
):
    """查询角色已分配的权限 id 列表。"""
    data = svc.get_permission_ids(role_id)
    return Response.ok(data=data)


@router.put("/{role_id}/permissions", response_model=Response)
async def assign_role_permissions(
    role_id: int,
    body: RolePermissionAssign,
    _: TokenData = Depends(require_permission("system:role:save")),
):
    """全量覆盖角色-权限关联（先清后插）。"""
    svc.assign_permissions(role_id, body.permissionIds)
    return Response.ok(data=role_id)
