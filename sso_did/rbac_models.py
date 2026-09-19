from typing import Any

from pydantic import BaseModel, Field


# ================= 通用 =================
class Response(BaseModel):
    """标准 API 响应包装。前端 getData 期望 code==200 取 data。"""
    code: int = 200
    message: str = "success"
    data: Any = None

    @classmethod
    def ok(cls, data: Any = None) -> "Response":
        return cls(data=data)

    @classmethod
    def fail(cls, code: int = 500, message: str = "error") -> "Response":
        return cls(code=code, message=message)


class PageQuery(BaseModel):
    pageNo: int = Field(default=1, ge=1)
    pageSize: int = Field(default=10, ge=1, le=500)
    sortField: str | None = None
    sortAsc: bool = True


class PageData(BaseModel):
    data: list = []
    pageNo: int
    pageSize: int
    total: int | None = None
    pages: int | None = None


# ================= User =================
class UserCreate(BaseModel):
    uid: str
    displayName: str
    email: str | None = None
    mobile: str | None = None
    deptCode: str | None = None
    department: str | None = None
    employeeType: str | None = None
    status: str = "Enabled"
    roleIds: list[int] = []


class UserResponse(BaseModel):
    id: int
    uid: str
    displayName: str | None = None
    email: str | None = None
    mobile: str | None = None
    deptCode: str | None = None
    department: str | None = None
    employeeType: str | None = None
    status: str = "Enabled"
    roleIds: list[int] = []

    class Config:
        from_attributes = True


class UserQuery(PageQuery):
    uid: str | None = None
    displayName: str | None = None
    email: str | None = None
    department: str | None = None
    status: str | None = None


# ================= Role =================
class RoleCreate(BaseModel):
    code: str
    name: str
    description: str | None = None
    status: str = "Enabled"


class RoleResponse(BaseModel):
    id: int
    code: str
    name: str
    description: str | None = None
    status: str = "Enabled"
    permissionIds: list[int] = []   # 角色已分配的权限 id，角色详情回显用

    class Config:
        from_attributes = True


class RoleQuery(PageQuery):
    status: str | None = None
    name: str | None = None


# ================= Permission =================
class PermissionCreate(BaseModel):
    code: str
    name: str
    action: str = "read"        # read / create / update / delete
    type: str = "menu"          # menu / api / data
    description: str | None = None
    status: str = "Enabled"


class PermissionResponse(BaseModel):
    id: int
    code: str
    name: str
    action: str
    type: str
    description: str | None = None
    status: str = "Enabled"

    class Config:
        from_attributes = True


class PermissionQuery(PageQuery):
    status: str | None = None
    name: str | None = None
    code: str | None = None
    type: str | None = None


class RolePermissionAssign(BaseModel):
    """角色分配权限（整批覆盖，对齐 _assign_roles 的全量替换风格）。"""
    permissionIds: list[int] = []


# ================= Auth =================
class FullUserResponse(BaseModel):
    """当前登录用户的完整画像（JWT 层 + sys_user 库表合并）。

    供 /users/current-user 与生成链路等业务调用：路由层经 UserService.get_full_user()
    取得，节点层经 ChatState['user'] 取（路由入口已塞入）。
    roles/permissions 来自 JWT（签发时已查库），其余字段来自 sys_user 行。
    """
    uid: str                                  # account_id（JWT.sub）
    username: str | None = None               # display_name（JWT.username）
    roles: list[str] = []                     # 角色 codes（JWT.roles）
    permissions: list[str] = []               # 权限码（JWT.permissions）
    email: str | None = None                  # sys_user.email
    mobile: str | None = None                 # sys_user.mobile
    deptCode: str | None = None               # sys_user.dept_code
    department: str | None = None             # sys_user.department
    employeeType: str | None = None            # sys_user.employee_type
    status: str = "Enabled"                   # sys_user.status
    userId: int | None = None                 # sys_user.id（库内主键，可能未建档）
